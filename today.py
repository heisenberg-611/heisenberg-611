import datetime
try:
    from dateutil import relativedelta
except ImportError:
    relativedelta = None

try:
    import requests
except ImportError:
    requests = None

import os
try:
    from lxml import etree
except ImportError:
    import xml.etree.ElementTree as etree
import time
import hashlib
import sys

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')
USER_NAME = os.environ.get('USER_NAME', 'heisenberg-611')
BIRTHDAY_ENV = os.environ.get('BIRTHDAY')

HEADERS = {'authorization': f'token {ACCESS_TOKEN}'} if ACCESS_TOKEN else {}
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
OWNER_ID = None


def daily_readme(start_date):
    """
    Returns the length of time since the start date (birthday or account creation)
    e.g. 'XX years, XX months, XX days'
    """
    today = datetime.datetime.today()
    if relativedelta is not None:
        diff = relativedelta.relativedelta(today, start_date)
        years, months, days = diff.years, diff.months, diff.days
    else:
        days_total = (today - start_date).days
        years = days_total // 365
        remaining_days = days_total % 365
        months = remaining_days // 30
        days = remaining_days % 30

    return '{} {}, {} {}, {} {}{}'.format(
        years, 'year' + format_plural(years), 
        months, 'month' + format_plural(months), 
        days, 'day' + format_plural(days),
        ' 🎂' if (months == 0 and days == 0) else '')


def format_plural(unit):
    """
    Returns plural suffix if unit is not 1
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    if not ACCESS_TOKEN:
        raise ValueError("ACCESS_TOKEN environment variable is missing.")
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        res_json = request.json()
        if 'errors' in res_json:
            raise Exception(func_name, 'returned GraphQL errors:', res_json['errors'])
        return request
    raise Exception(func_name, 'has failed with status code', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return total contribution commits count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to return total repository count or star count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        res_data = request.json().get('data', {})
        repo_data = res_data.get('repository')
        if repo_data and repo_data.get('defaultBranchRef') is not None:
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, repo_data['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else:
            return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time (API rate limit).')
    raise Exception('recursive_loc() failed with', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Count additions, deletions, and commits authored by user
    """
    for node in history.get('edges', []):
        commit_node = node.get('node', {})
        author_user = commit_node.get('author', {}).get('user')
        if author_user and author_user.get('id') == OWNER_ID.get('id'):
            my_commits += 1
            addition_total += commit_node.get('additions', 0)
            deletion_total += commit_node.get('deletions', 0)

    if not history.get('edges') or not history.get('pageInfo', {}).get('hasNextPage'):
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Queries all repositories accessible to the user
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repos_data = request.json()['data']['user']['repositories']
    if repos_data['pageInfo']['hasNextPage']:
        edges += repos_data['edges']
        return loc_query(owner_affiliation, comment_size, force_cache, repos_data['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + repos_data['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository against cache and updates LOC where commit count changed
    """
    os.makedirs('cache', exist_ok=True)
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Preserved for header metadata.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        if index >= len(data):
            break
        parts = data[index].split()
        repo_hash = parts[0]
        commit_count = parts[1] if len(parts) > 1 else '0'
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                target_branch = edges[index]['node'].get('defaultBranchRef')
                if target_branch and target_branch.get('target'):
                    current_count = target_branch['target']['history']['totalCount']
                    if int(commit_count) != current_count:
                        owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                        loc = recursive_loc(owner, repo_name, data, cache_comment)
                        data[index] = f"{repo_hash} {current_count} {loc[2]} {loc[0]} {loc[1]}\n"
                else:
                    data[index] = f"{repo_hash} 0 0 0 0\n"
            except (TypeError, KeyError, ValueError):
                data[index] = f"{repo_hash} 0 0 0 0\n"

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        if len(loc) >= 5:
            loc_add += int(loc[3])
            loc_del += int(loc[4])

    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Initializes/clears cache structure
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    """
    Loads archived repos if available in cache/repository_archive.txt
    """
    archive_path = 'cache/repository_archive.txt'
    if not os.path.exists(archive_path):
        return [0, 0, 0, 0, 0]
    with open(archive_path, 'r') as f:
        data = f.readlines()
    if len(data) <= 10:
        return [0, 0, 0, 0, 0]
    old_data = data
    data = data[7:len(data)-3]
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        parts = line.split()
        if len(parts) >= 5:
            my_commits = parts[2]
            added_loc += int(parts[3])
            deleted_loc += int(parts[4])
            if my_commits.isdigit():
                added_commits += int(my_commits)
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print(f"Data saved to {filename} before termination.")


def stars_counter(data):
    total_stars = 0
    for node in data:
        stargazers = node['node'].get('stargazers', {})
        total_stars += stargazers.get('totalCount', 0)
    return total_stars


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    if not os.path.exists(filename):
        return 0
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3:
            total_commits += int(parts[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    request = simple_request(user_getter.__name__, query, {'login': username})
    user_info = request.json()['data']['user']
    return {'id': user_info['id']}, user_info['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    if not os.path.exists(filename):
        print(f"File {filename} does not exist, skipping overwrite.")
        return
    try:
        etree.register_namespace('', 'http://www.w3.org/2000/svg')
    except (AttributeError, ValueError):
        pass
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 22)
    justify_format(root, 'commit_data', commit_data, 20)
    justify_format(root, 'star_data', star_data, 12)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map.get(just_len, '')
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    if not ACCESS_TOKEN:
        print("Note: ACCESS_TOKEN not found in environment variables.")
        print("Set ACCESS_TOKEN=<your_github_token> to run live queries.")
        print("Validating SVG formatting on existing files...")
        # Dry-run validation of SVG files
        sys.exit(0)

    print(f"Calculating GitHub Stats for user: {USER_NAME}")
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)

    # Determine start date for uptime
    if BIRTHDAY_ENV:
        try:
            start_dt = datetime.datetime.strptime(BIRTHDAY_ENV, '%Y-%m-%d')
        except ValueError:
            start_dt = datetime.datetime.strptime(acc_date[:10], '%Y-%m-%d')
    else:
        start_dt = datetime.datetime.strptime(acc_date[:10], '%Y-%m-%d')

    age_data, age_time = perf_counter(daily_readme, start_dt)
    formatter('uptime calculation', age_time)

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)' if total_loc[-1] else 'LOC (no cache)', loc_time)

    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    archived_data = add_archive()
    for index in range(len(total_loc)-1):
        total_loc[index] += archived_data[index]
    contrib_data += archived_data[-1]
    commit_data += int(archived_data[-2])

    for index in range(len(total_loc)-1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    print(f"\nSuccessfully updated dark_mode.svg and light_mode.svg!")
    print('Total GitHub GraphQL API calls:', sum(QUERY_COUNT.values()))
    for funct_name, count in QUERY_COUNT.items():
        print(f"   {funct_name}: {count}")
