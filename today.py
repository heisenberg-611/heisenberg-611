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
import traceback

# Increase recursion limit as a fallback precaution
sys.setrecursionlimit(5000)

# Force line-buffered stdout/stderr. Without this, prints sit in an internal
# buffer when stdout is piped (as it is in CI) and are LOST if the process is
# killed abruptly (e.g. an OOM-kill) instead of exiting normally. This is why
# earlier runs showed no traceback even after a crash: the traceback text was
# printed but never flushed before the process died.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except Exception:
        pass

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN')
USER_NAME = os.environ.get('USER_NAME', 'heisenberg-611')
BIRTHDAY_ENV = os.environ.get('BIRTHDAY')

HEADERS = {'authorization': f'token {ACCESS_TOKEN}'} if ACCESS_TOKEN else {}
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
OWNER_ID = None

# Small pause between GraphQL calls to avoid tripping GitHub's secondary/abuse
# rate limiter, which fires on request *rate*, independent of the 5000/hr quota.
REQUEST_DELAY = 0.35
# Cooldown when we detect a secondary rate limit / abuse-detection response.
SECONDARY_RATE_LIMIT_SLEEP = 60


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


SESSION = requests.Session() if requests else None


def _is_secondary_rate_limit(status_code, text):
    """
    Detects GitHub's secondary/abuse-detection rate limit, which needs a much
    longer cooldown than a normal 403/429 and is otherwise indistinguishable
    from other 403s by status code alone.
    """
    if status_code not in (403, 429):
        return False
    lowered = (text or '').lower()
    return 'secondary rate limit' in lowered or 'abuse detection' in lowered


def simple_request(func_name, query, variables, retries=3):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    if not ACCESS_TOKEN:
        raise ValueError("ACCESS_TOKEN environment variable is missing.")
    client = SESSION if SESSION is not None else requests

    request = None
    for attempt in range(retries + 1):
        try:
            request = client.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS, timeout=30)
        except Exception as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise e

        if request.status_code == 200:
            res_json = request.json()
            if 'errors' in res_json:
                # Some GraphQL errors are per-node (e.g. a repo the token can't
                # see) and non-fatal for the overall query. Only hard-fail if
                # there's no usable data at all.
                if not res_json.get('data'):
                    raise Exception(func_name, 'returned GraphQL errors with no data:', res_json['errors'])
                print(f"Warning: {func_name} returned partial GraphQL errors: {res_json['errors']}")
            time.sleep(REQUEST_DELAY)
            return request
        elif _is_secondary_rate_limit(request.status_code, request.text):
            print(f"Warning: secondary rate limit hit in {func_name}, cooling down {SECONDARY_RATE_LIMIT_SLEEP}s...")
            time.sleep(SECONDARY_RATE_LIMIT_SLEEP)
            continue
        elif request.status_code in (403, 429, 502, 503) and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue
        break

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


def rest_repo_stars(username):
    """
    REST fallback for star counts. Fine-grained PATs currently have a known
    gap where the GraphQL `stargazers` field returns a FORBIDDEN error even
    with Metadata read access granted. The equivalent REST endpoint
    (GET /users/{username}/repos) works fine under the same token, so we use
    that instead for this one value.
    """
    if not requests:
        return 0
    client = SESSION if SESSION is not None else requests
    total_stars = 0
    page = 1
    while True:
        resp = client.get(
            f'https://api.github.com/users/{username}/repos',
            headers=HEADERS,
            params={'type': 'owner', 'per_page': 100, 'page': page},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Warning: REST stars fallback failed with status {resp.status_code}: {resp.text[:200]}")
            break
        repos = resp.json()
        if not repos:
            break
        total_stars += sum(r.get('stargazers_count', 0) for r in repos)
        if len(repos) < 100:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return total_stars


def graph_repos_stars(count_type, owner_affiliation):
    """
    Uses GitHub's GraphQL v4 API to return total repository count or star count across all pages.
    """
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
    cursor = None
    total_stars = 0
    while True:
        query_count('graph_repos_stars')
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        request = simple_request(graph_repos_stars.__name__, query, variables)
        user_data = request.json()['data']['user']['repositories']
        if count_type == 'repos':
            return user_data['totalCount']
        elif count_type == 'stars':
            total_stars += stars_counter(user_data.get('edges', []))

        page_info = user_data.get('pageInfo', {})
        new_cursor = page_info.get('endCursor')
        if page_info.get('hasNextPage') and new_cursor and new_cursor != cursor:
            cursor = new_cursor
        else:
            break

    return total_stars


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time iteratively,
    filtering by author ID directly on GitHub to avoid fetching unrelated commits from organizations/collaborators.
    """
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String, $author_id: ID!) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor, author: { id: $author_id }) {
                            totalCount
                            edges {
                                node {
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

    client = SESSION if SESSION is not None else requests
    author_id = OWNER_ID.get('id') if isinstance(OWNER_ID, dict) else OWNER_ID
    retries_left = 3

    while True:
        query_count('recursive_loc')
        variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor, 'author_id': author_id}
        try:
            request = client.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS, timeout=30)
        except Exception as e:
            if retries_left > 0:
                retries_left -= 1
                time.sleep(3)
                continue
            print(f"Warning: Network error for {owner}/{repo_name}: {e}. Skipping remainder.")
            return addition_total, deletion_total, my_commits

        if request.status_code == 200:
            retries_left = 3  # reset retries on success
            time.sleep(REQUEST_DELAY)
            res_json = request.json()
            if 'errors' in res_json and not res_json.get('data'):
                print(f"Warning: GraphQL error in {owner}/{repo_name}: {res_json['errors']}")
                return addition_total, deletion_total, my_commits

            res_data = res_json.get('data', {})
            repo_data = res_data.get('repository')
            if not repo_data or not repo_data.get('defaultBranchRef') or not repo_data['defaultBranchRef'].get('target'):
                return addition_total, deletion_total, my_commits

            history = repo_data['defaultBranchRef']['target'].get('history')
            if not history:
                return addition_total, deletion_total, my_commits

            edges = history.get('edges', [])
            for node in edges:
                commit_node = node.get('node', {})
                my_commits += 1
                addition_total += commit_node.get('additions', 0)
                deletion_total += commit_node.get('deletions', 0)

            page_info = history.get('pageInfo', {})
            new_cursor = page_info.get('endCursor')
            if not edges or not page_info.get('hasNextPage') or not new_cursor or new_cursor == cursor:
                return addition_total, deletion_total, my_commits
            cursor = new_cursor
        elif _is_secondary_rate_limit(request.status_code, request.text):
            print(f"Warning: secondary rate limit hit on {owner}/{repo_name}, cooling down {SECONDARY_RATE_LIMIT_SLEEP}s...")
            time.sleep(SECONDARY_RATE_LIMIT_SLEEP)
            continue
        elif request.status_code in (403, 429, 502, 503):
            if retries_left > 0:
                retries_left -= 1
                time.sleep(3 * (4 - retries_left))
                continue
            print(f"Warning: GitHub API returned status {request.status_code} for {owner}/{repo_name}. Skipping remainder.")
            return addition_total, deletion_total, my_commits
        else:
            print(f"Warning: Unexpected status {request.status_code} for {owner}/{repo_name}. Skipping remainder.")
            return addition_total, deletion_total, my_commits


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Queries all repositories accessible to the user iteratively.
    """
    if edges is None:
        edges = []

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

    while True:
        query_count('loc_query')
        variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
        request = simple_request(loc_query.__name__, query, variables)
        repos_data = request.json()['data']['user']['repositories']
        edges.extend(repos_data.get('edges', []))
        page_info = repos_data.get('pageInfo', {})
        new_cursor = page_info.get('endCursor')
        if page_info.get('hasNextPage') and new_cursor and new_cursor != cursor:
            cursor = new_cursor
        else:
            break

    return cache_builder(edges, comment_size, force_cache)


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
        repo_name_with_owner = edges[index]['node']['nameWithOwner']
        if repo_hash == hashlib.sha256(repo_name_with_owner.encode('utf-8')).hexdigest():
            try:
                target_branch = edges[index]['node'].get('defaultBranchRef')
                if target_branch and target_branch.get('target') and target_branch['target'].get('history'):
                    current_count = target_branch['target']['history']['totalCount']
                    if int(commit_count) != current_count:
                        print(f"   Updating LOC [{index+1}/{len(edges)}]: {repo_name_with_owner} ({current_count} commits)", flush=True)
                        owner, repo_name = repo_name_with_owner.split('/')
                        loc = recursive_loc(owner, repo_name, data, cache_comment)
                        data[index] = f"{repo_hash} {current_count} {loc[2]} {loc[0]} {loc[1]}\n"
                else:
                    data[index] = f"{repo_hash} 0 0 0 0\n"
            except (TypeError, KeyError, ValueError):
                data[index] = f"{repo_hash} 0 0 0 0\n"
            finally:
                # Persist progress after every repo, so a crash mid-run still
                # leaves a usable, up-to-date cache for the next run instead
                # of losing everything computed so far.
                with open(filename, 'w') as f:
                    f.writelines(cache_comment)
                    f.writelines(data)

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
        node_data = node.get('node')
        if not node_data:
            # GitHub can return a null node when a nested field (e.g.
            # stargazers) hit a FORBIDDEN error for that repo.
            continue
        stargazers = node_data.get('stargazers') or {}
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
    # age_data intentionally does NOT use justify_format here. The dot-leader
    # for this row is now a fixed length, hand-aligned so its value starts at
    # the same column as every other row in the panel. justify_format would
    # instead resize the dots based on the value's length each run (right-
    # justifying the END of the text), which drifts the START out of
    # alignment with the rest of the panel every time the day-count's digit
    # count changes. Only the value text itself needs updating.
    find_and_replace(root, 'age_data', age_data)
    justify_format(root, 'commit_data', commit_data, 21)
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


def safe_perf_counter(funct, fallback, *args, label=None):
    """
    Like perf_counter, but a failure here (network hiccup, rate limit, etc.)
    logs a full traceback and falls back to a default value instead of
    killing the entire run. Used for the "nice to have" stats (stars, repos,
    contrib count, followers) where partial data is far better than no SVG
    update at all.
    """
    start = time.perf_counter()
    try:
        result = funct(*args)
        return result, time.perf_counter() - start
    except Exception:
        print(f"Warning: {label or funct.__name__} failed, using fallback value {fallback!r}.")
        traceback.print_exc()
        return fallback, time.perf_counter() - start


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
        sys.exit(0)

    try:
        print(f"Calculating GitHub Stats for user: {USER_NAME}")
        user_data, user_time = perf_counter(user_getter, USER_NAME)
        OWNER_ID, acc_date = user_data
        formatter('account data', user_time)

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

        # Everything below is "nice to have" — a transient failure on any one
        # of these (rate limit, network blip) should not prevent the SVGs
        # from being updated with everything we DO have.
        commit_data, commit_time = safe_perf_counter(commit_counter, 0, 7, label='commit_counter')
        star_data, star_time = safe_perf_counter(rest_repo_stars, 0, USER_NAME, label='rest_repo_stars')
        repo_data, repo_time = safe_perf_counter(graph_repos_stars, 0, 'repos', ['OWNER'], label='graph_repos_stars(repos)')
        contrib_data, contrib_time = safe_perf_counter(graph_repos_stars, 0, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], label='graph_repos_stars(contrib)')
        follower_data, follower_time = safe_perf_counter(follower_getter, 0, USER_NAME, label='follower_getter')

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

    except (Exception, KeyboardInterrupt):
        # Print the FULL traceback so the Actions log shows exactly which
        # call and line failed, instead of just the generic exit-code-1 line.
        # KeyboardInterrupt is included because a job timeout/cancellation
        # arrives as SIGTERM -> KeyboardInterrupt, which plain `except
        # Exception` would silently miss.
        print("\nFATAL: today.py crashed. Full traceback below:", flush=True)
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(1)