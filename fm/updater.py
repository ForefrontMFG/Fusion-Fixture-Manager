"""Publish this add-in to its GitHub repo and install published versions, using git.

No Fusion dependency, so it can be unit-tested. The add-in folder is a git clone of the repo;
a published version is a commit on `main` tagged `v<major>.<minor>.<patch>`, with the same
version in FixtureManager.manifest. Only the add-in code is in the repo: config.json (this
machine's library path) and the fixture library itself stay local.
"""

import json
import os
import re
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, 'FixtureManager.manifest')
BRANCH = 'main'
REMOTE = 'origin'
PARTS = ('patch', 'minor', 'major')

_TAG_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')
_VERSION_LINE_RE = re.compile(r'("version"\s*:\s*")([^"]*)(")')
_GIT_CANDIDATES = ('/usr/bin/git', '/opt/homebrew/bin/git', '/usr/local/bin/git',
                   r'C:\Program Files\Git\cmd\git.exe', r'C:\Program Files (x86)\Git\cmd\git.exe')


class UpdateError(Exception):
    pass


# ------------------------------------------------------------------ versions (pure)

def parse_version(text):
    """(major, minor, patch) from '1.2.3' or 'v1.2.3', else None."""
    m = _TAG_RE.match((text or '').strip())
    return tuple(int(g) for g in m.groups()) if m else None


def bump(version, part='patch'):
    v = parse_version(version)
    if v is None:
        raise UpdateError('"{}" is not a version like 1.2.3.'.format(version))
    if part == 'major':
        v = (v[0] + 1, 0, 0)
    elif part == 'minor':
        v = (v[0], v[1] + 1, 0)
    elif part == 'patch':
        v = (v[0], v[1], v[2] + 1)
    else:
        raise UpdateError('Unknown version part "{}".'.format(part))
    return '{}.{}.{}'.format(*v)


def latest_version(tags):
    """The highest version among tags like 'v1.2.3' (others are ignored), as '1.2.3', or None."""
    versions = [v for v in (parse_version(t) for t in tags) if v]
    return '{}.{}.{}'.format(*max(versions)) if versions else None


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and (b is None or a > b))


def set_manifest_version(text, version):
    """The manifest text with its version replaced; keeps the file's own formatting."""
    new, n = _VERSION_LINE_RE.subn(lambda m: m.group(1) + version + m.group(3), text, count=1)
    if n != 1:
        raise UpdateError('FixtureManager.manifest has no "version" entry.')
    return new


# ------------------------------------------------------------------ manifest

def read_version(root=ROOT):
    with open(os.path.join(root, 'FixtureManager.manifest'), 'r', encoding='utf-8') as f:
        return json.load(f).get('version', '0.0.0')


def _write_version(version, root=ROOT):
    path = os.path.join(root, 'FixtureManager.manifest')
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(set_manifest_version(text, version))


# ------------------------------------------------------------------ git

def find_git():
    # Fusion starts add-ins with a short PATH, so look in the usual install places too.
    found = shutil.which('git')
    if found:
        return found
    for path in _GIT_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise UpdateError('git was not found. Install git (on a Mac: run "xcode-select --install"; on '
                      'Windows: git-scm.com) and try again.')


def _git(*args, root=ROOT, check=True, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')  # fail instead of waiting for a password
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
    try:
        res = subprocess.run([find_git(), '-C', root] + list(args), capture_output=True, text=True,
                             env=env, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise UpdateError('git {} timed out. Check the network connection.'.format(args[0]))
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        if 'Authentication failed' in detail or 'could not read Username' in detail:
            detail += ('\n\nThis computer is not signed in to GitHub for git. Run "gh auth login" '
                       'then "gh auth setup-git" in a terminal, or set up a credential helper.')
        raise UpdateError('git {} failed:\n{}'.format(' '.join(args[:2]), detail))
    return res.stdout.strip()


def _require_repo(root=ROOT):
    if not os.path.isdir(os.path.join(root, '.git')):
        raise UpdateError('This add-in folder is not a git clone of the Fixture Manager repo:\n{}\n\n'
                          'Install it with "git clone" (see the README) to publish or update.'.format(root))
    if not _git('remote', root=root, check=False):
        raise UpdateError('The add-in repo has no "{}" remote to publish to.'.format(REMOTE))


def changed_files(root=ROOT):
    out = _git('status', '--porcelain', root=root)
    return [line[3:] for line in out.splitlines() if line.strip()]


def _ahead_behind(root=ROOT):
    """(local commits not on GitHub, GitHub commits not here) for main."""
    out = _git('rev-list', '--left-right', '--count', 'HEAD...{}/{}'.format(REMOTE, BRANCH),
               root=root, check=False)
    try:
        ahead, behind = (int(x) for x in out.split())
    except ValueError:
        return 0, 0  # nothing published yet
    return ahead, behind


def status(fetch=True, root=ROOT):
    """A summary for the dialogs. Never raises for a missing repo: 'error' says what's wrong."""
    info = {'version': read_version(root), 'root': root, 'is_repo': False, 'error': ''}
    try:
        _require_repo(root)
        info['is_repo'] = True
        info['remote'] = _git('remote', 'get-url', REMOTE, root=root, check=False)
        if fetch:
            _git('fetch', '--tags', '--prune', REMOTE, root=root)
        info['changed'] = changed_files(root)
        info['ahead'], info['behind'] = _ahead_behind(root)
        info['latest'] = latest_version(_git('tag', '--list', 'v*', root=root).splitlines())
    except UpdateError as ex:
        info['error'] = str(ex)
    return info


def describe(info):
    if not info['is_repo'] or info['error']:
        return 'Version {}\n\n{}'.format(info['version'], info['error'])
    lines = ['Installed version: {}'.format(info['version']),
             'Latest published: {}'.format(info.get('latest') or 'none yet'),
             'Repo: {}'.format(info.get('remote', ''))]
    changed = info.get('changed') or []
    if changed:
        lines.append('Changed since the last publish ({}): {}'.format(
            len(changed), ', '.join(changed[:8]) + (' ...' if len(changed) > 8 else '')))
    else:
        lines.append('No changes since the last publish.')
    if info.get('ahead'):
        lines.append('{} commit(s) not pushed to GitHub yet.'.format(info['ahead']))
    if info.get('behind'):
        lines.append('GitHub has {} newer commit(s): check for updates first.'.format(info['behind']))
    return '\n'.join(lines)


# ------------------------------------------------------------------ publish / update

def publish(note, part='patch', root=ROOT):
    """Bump the version, commit everything, tag it and push to GitHub. Returns messages."""
    note = (note or '').strip()
    if not note:
        raise UpdateError('Write a short note saying what changed in this version.')
    _require_repo(root)
    _git('fetch', '--tags', REMOTE, root=root)
    ahead, behind = _ahead_behind(root)
    if behind:
        raise UpdateError('GitHub has {} commit(s) this copy doesn\'t have (published from another '
                          'computer). Check for updates first, then publish.'.format(behind))
    changed = changed_files(root)
    if not changed and ahead:
        # An earlier publish committed but couldn't push: just push it.
        return _push(root, read_version(root), ['Pushed the version that was waiting to upload.'])
    if not changed:
        raise UpdateError('Nothing has changed since version {} was published.'.format(read_version(root)))
    current = read_version(root)
    tags = _git('tag', '--list', 'v*', root=root).splitlines()
    base = max([current] + [t[1:] for t in tags if parse_version(t)], key=parse_version)
    new = bump(base, part)
    _write_version(new, root)
    _git('add', '--all', root=root)
    _git('commit', '-m', 'v{}: {}'.format(new, note), root=root)
    _git('tag', '-a', 'v' + new, '-m', note, root=root)
    return _push(root, new, ['Committed {} changed file(s) as version {}.'.format(len(changed), new)])


def _push(root, version, msgs):
    try:
        _git('push', REMOTE, 'HEAD:' + BRANCH, root=root)
        _git('push', REMOTE, '--tags', root=root)
    except UpdateError as ex:
        raise UpdateError('{}\n\nVersion {} is saved on this computer; publish again to retry the '
                          'upload.'.format(ex, version))
    msgs.append('Published version {} to {}.'.format(version, _git('remote', 'get-url', REMOTE, root=root)))
    return msgs


def available_update(root=ROOT):
    """(latest published version, [release notes newer than this copy]) or (None, [])."""
    _require_repo(root)
    _git('fetch', '--tags', '--prune', REMOTE, root=root)
    current = read_version(root)
    tags = _git('tag', '--list', 'v*', '--format=%(refname:short)\t%(contents:subject)',
                root=root).splitlines()
    notes = []
    for line in tags:
        tag, _, subject = line.partition('\t')
        if is_newer(tag, current):
            notes.append((parse_version(tag), '{}: {}'.format(tag, subject)))
    latest = latest_version([t.partition('\t')[0] for t in tags])
    _, behind = _ahead_behind(root)
    if not behind or not is_newer(latest, current):
        return None, []
    return latest, [n for _, n in sorted(notes)]


def install_update(root=ROOT):
    """Fast-forward this copy to the latest published version. Refuses to touch local edits."""
    _require_repo(root)
    changed = changed_files(root)
    if changed:
        raise UpdateError('This copy has unpublished changes, so updating could overwrite them:\n{}\n\n'
                          'Publish them first, or discard them.'.format('\n'.join(changed[:12])))
    _git('fetch', '--tags', REMOTE, root=root)
    before = read_version(root)
    _git('merge', '--ff-only', '{}/{}'.format(REMOTE, BRANCH), root=root)
    after = read_version(root)
    return ['Updated from version {} to {}.'.format(before, after),
            'Restart the add-in to load it: Utilities > Add-Ins > Scripts and Add-Ins, select '
            'FixtureManager, Stop, then Run.']
