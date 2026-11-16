"""Remove release assets that cannot be tied to a trusted Kinora gate record."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

from release_gate import GateError, check_bytes, protected_addresses

from release_record import MANIFEST, SIGNATURE, RECORD_FILES, signature_valid

BASELINE = Path(__file__).with_name('release_baseline.json')
MAX_RECORD_SIZE = 128 * 1024


class ReconcileError(Exception):
    pass


def metadata(asset):
    return {'digest': asset['digest'], 'size': asset['size']}


def compare(assets, approved):
    found = set()
    remove = []
    for asset in assets:
        name = asset['name']
        if asset['state'] == 'uploaded' and name not in found and approved.get(name) == metadata(asset):
            found.add(name)
        else:
            remove.append(asset['id'])
    return sorted(remove), sorted(approved.keys() - found)


class GitHub:
    def __init__(self, repo):
        self.repo = repo
        self.prefix = f'repos/{repo}/'

    def raw(self, path, *args, data=None):
        try:
            response = subprocess.run(
                ['gh', 'api', self.prefix + path, *args],
                input=json.dumps(data).encode() if data is not None else None,
                capture_output=True, timeout=55,
            )
        except subprocess.SubprocessError as error:
            raise ReconcileError('GitHub API request timed out or failed') from error
        if response.returncode:
            raise ReconcileError('GitHub API request failed')
        return response.stdout

    def api(self, path, *args, data=None):
        body = self.raw(path, *args, data=data)
        return json.loads(body) if body else None

    def list(self, path):
        pages = self.api(path, '--paginate', '--slurp')
        return [item for page in pages for item in page]

    def assets(self, release):
        return self.list(f'releases/{release}/assets?per_page=100')

    def read_record_asset(self, asset):
        if not 0 < asset['size'] <= MAX_RECORD_SIZE:
            return None
        data = self.raw(f'releases/assets/{asset["id"]}', '-H', 'Accept: application/octet-stream')
        if (len(data) != asset['size']
                or 'sha256:' + hashlib.sha256(data).hexdigest() != asset['digest']):
            raise ReconcileError('Release record bytes changed or could not be fetched')
        return data

    def notify(self, owner, release_id, reason, assets, missing, result):
        key = hashlib.sha256(json.dumps([release_id, reason, assets, missing]).encode()).hexdigest()[:12]
        title = f'Kinora release guard {release_id} [{key}]'
        for issue in self.list('issues?state=open&per_page=100'):
            if 'pull_request' not in issue and issue['title'] == title:
                return issue['number'], False
        run = os.environ.get('GITHUB_RUN_ID', '')
        link = f'\n\nWorkflow run: https://github.com/{self.repo}/actions/runs/{run}' if run.isdecimal() else ''
        body = (f'@{owner} Kinora release ID {release_id} did not match its approved release record.\n\n'
                f'{result} Unapproved or changed attachments: {len(assets)}. '
                f'Missing approved attachments: {len(missing)}.{link}')
        issue = self.api('issues', '-X', 'POST', '--input', '-',
                         data={'title': title, 'body': body, 'assignees': [owner]})
        return issue['number'], True


def trusted_assets(github, release, assets, legacy):
    entry = legacy.get(str(release['id']))
    if entry:
        return entry['assets'] if entry['tag'] == release['tag_name'] else None

    matched = {name: [a for a in assets if a['name'] == name and a['state'] == 'uploaded']
               for name in RECORD_FILES}
    if any(len(items) != 1 for items in matched.values()):
        return None
    record_asset = matched[MANIFEST][0]
    signature_asset = matched[SIGNATURE][0]
    if signature_asset['size'] != 64:
        return None
    record = github.read_record_asset(record_asset)
    signature = github.read_record_asset(signature_asset)
    if record is None or signature is None or not signature_valid(record, signature):
        return None
    try:
        document = json.loads(record)
        if (document['schema'] != 1 or document['repository'] != github.repo
                or document['release_id'] != release['id']
                or document['tag'] != release['tag_name']
                or document['commit'] != release['target_commitish']):
            return None
        files = document['assets']
        if (not isinstance(files, dict) or not files or RECORD_FILES.intersection(files)
                or any(not isinstance(name, str) or not isinstance(value['size'], int)
                       or value['size'] < 0 or not re.fullmatch(r'sha256:[a-f0-9]{64}', value['digest'])
                       for name, value in files.items())):
            return None
        return {**files, MANIFEST: metadata(record_asset), SIGNATURE: metadata(signature_asset)}
    except (KeyError, TypeError, ValueError):
        return None


def snapshot(assets):
    return sorted((item['id'], item['name'], item['state'], item['digest'], item['size']) for item in assets)


def unsafe_release_text(release, blocked):
    replacements = {}
    for field, replacement in (
        ('name', 'Kinora release ' + str(release['id'])),
        ('body', 'Release notes removed after the disclosure check.'),
    ):
        value = release.get(field) or ''
        try:
            check_bytes(html.unescape(unquote(value)).encode(), blocked)
        except GateError:
            replacements[field] = replacement
    return replacements


def reconcile(github, release, baseline, owner, dry_run, blocked=()):
    release_id = release['id']
    assets = github.assets(release_id)
    approved = trusted_assets(github, release, assets, baseline)
    redact = unsafe_release_text(release, blocked)
    if approved is None:
        bad, missing = sorted(asset['id'] for asset in assets), []
    else:
        bad, missing = compare(assets, approved)
        if not (bad or missing or redact):
            return False

    withdraw = approved is None or (release['immutable'] and bool(bad))
    reason = 'release' if withdraw else ('release text' if redact else 'attachments')
    print(f'Release ID {release_id}: {reason}, {len(bad)} unapproved, {len(missing)} missing, '
          f'{len(redact)} text fields flagged', flush=True)
    if dry_run:
        return True

    # Re-read before mutation so a manual change during inspection is retried on the next check.
    current = github.api(f'releases/{release_id}')
    if (current['draft'] or current['tag_name'] != release['tag_name']
            or current['immutable'] != release['immutable']
            or current['target_commitish'] != release['target_commitish']
            or (redact and any(current.get(field) != release.get(field) for field in ('name', 'body')))
            or snapshot(github.assets(release_id)) != snapshot(assets)):
        raise ReconcileError('Release changed while being checked')
    result = 'The approved attachment set is incomplete.'
    if withdraw:
        result = 'The unverified Release was withdrawn.'
    elif redact and bad:
        result = 'Exposed release text and unapproved attachments were removed.'
    elif redact:
        result = 'Exposed release text was removed.'
    elif bad:
        result = 'Unapproved attachments were removed.'

    # Create the directly assigned incident before mutation so an API failure cannot lose the notice.
    if withdraw or bad or redact:
        issue = None
        try:
            issue, _ = github.notify(owner, release_id, reason, bad, missing,
                          'The guard detected unapproved content and is attempting withdrawal.')
        except ReconcileError:
            print(f'Release ID {release_id}: could not create issue notification', flush=True)
        if withdraw:
            github.api(f'releases/{release_id}', '-X', 'DELETE')
        else:
            if redact:
                github.api(f'releases/{release_id}', '-X', 'PATCH', '--input', '-', data=redact)
            for asset_id in bad:
                github.api(f'releases/assets/{asset_id}', '-X', 'DELETE')
        print(f'Release ID {release_id}: {result}', flush=True)
        if issue is not None:
            github.api(f'issues/{issue}/comments', '-X', 'POST', '--input', '-', data={'body': result})
        return True
    _, created = github.notify(owner, release_id, reason, bad, missing, result)
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    github = GitHub(args.repo)
    baseline = json.loads(BASELINE.read_text())
    owner = args.repo.split('/', 1)[0]
    incident = False
    try:
        blocked = protected_addresses()
        releases = github.list('releases?per_page=100')
        for release in releases:
            if release['draft']:
                continue
            try:
                incident |= reconcile(github, release, baseline, owner, args.dry_run, blocked)
            except (ReconcileError, OSError, subprocess.SubprocessError):
                print(f'Release ID {release["id"]}: verification or remediation needs a retry', flush=True)
                incident = True
    except (GateError, ReconcileError, OSError, ValueError):
        raise SystemExit('Kinora release reconciliation could not complete') from None
    if incident:
        raise SystemExit('Kinora release reconciliation detected a mismatch or incomplete check')
    print('All published Kinora releases match their approved attachment records and passed the text check')


if __name__ == '__main__':
    main()
