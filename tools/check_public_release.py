"""Offline privacy checks for publication payloads and complete local Git objects.

Prints locations/rule names only. Pair with an independent secret scanner such
as Gitleaks; this tool does not certify absence of every possible private fact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    'user-home-path': re.compile(r'(?i)(?:[a-z]:[\\/]+Users[\\/]+[^\s"\'<>\r\n]+|/(?:Users|home)/[^\s"\'<>\r\n]+)'),
    'email': re.compile(r'(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b'),
    'private-ip': re.compile(r'\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
    'credential-url': re.compile(r'https?://[^\s/]+:[^\s@]+@[^\s"\'<>]+'),
    'credential-literal': re.compile(r'''(?i)(?:api[_-]?key|access[_-]?token|secret|password)["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']'''),
    'provider-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'webhook': re.compile(r'https?://[^\s"\'<>]*(?:qyapi\.weixin\.qq\.com/cgi-bin/webhook|hooks\.slack\.com/services|discord(?:app)?\.com/api/webhooks)[^\s"\'<>]*'),
    'phone-shape': re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),
    'identity-shape': re.compile(r'(?<![A-Za-z0-9])(?:[1-6]\d{5})(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![A-Za-z0-9])'),
}
PRIVATE_ROOTS = {'.planning', '.workbuddy', '.zcode', 'private_audit', 'state', 'data', 'backups', 'archives', 'outputs', '.venv'}
PRIVATE_NAMES = {'PROJECT_MEMORY.md', 'AGENTS.md', 'llm_profiles.json', '.gitleaksignore'}
TEXT_SUFFIXES = {'.py', '.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.js', '.css', '.html', '.example'}
REVIEWED_BINARY = {
    'docs/diagrams/research-workflow.svg': '0edd2cfee6faa3c758d40af7fdd9a8936153e0446a350bbf0a1f63606ab3077f',
    'docs/diagrams/system-architecture.svg': '6537d3ef1dce6b88589cff9d68f8bfe0eb7cbe321e8d8c3108217d3a51f73875',
    'docs/images/ui-v3-overview-nav.jpg': '1b4b3b09ef4570f1ca28fd5643de4e65ba18bdae67c13f3a368fa1cfb23539ad',
    'docs/images/ui-v3-news-nav.jpg': 'a28f34542ade81bbee445d154fd54fbb2b1ba1bafe97b81d9c29e928ded6a0c5',
    'docs/images/ui-v3-picks-nav.jpg': 'd1d870815a3dce9c4b1f93d44f01d65c619a7ad065c543ed782544b2312a8cc2',
    'docs/images/ui-v3-overview.jpg': '1b4b3b09ef4570f1ca28fd5643de4e65ba18bdae67c13f3a368fa1cfb23539ad',
    'docs/images/ui-v3-news.jpg': 'a28f34542ade81bbee445d154fd54fbb2b1ba1bafe97b81d9c29e928ded6a0c5',
    'docs/images/ui-v3-picks.jpg': 'd1d870815a3dce9c4b1f93d44f01d65c619a7ad065c543ed782544b2312a8cc2',
    'models/ai_quant/calibrator_AIQ-V1.0.pkl': '8e6bbc3d27a155947d3b1df7bc1cd918f523dab960c081c9aa535ceba9931b9c',
    'docs/images/blogger-lab-knowledge.jpg': '316e5d0a0559d927d1e0a1dafe86a47340476aded9849fd8025398f627743232',
    'docs/images/blogger-lab-strategy.jpg': '00efdb68a626a14514d9fff44f6d15b12bde9a7e2fe848153987d96b08ad56c3',
    'docs/images/blogger-lab-scan.jpg': '8eb643ddfe3c6da082754f3c49c6f80b835ff8e8074f162956d2deb0ffb86086',
    'docs/images/blogger-lab-paper.jpg': 'e242b28f89fde4d249f2380a7c754c8177f9fdbccf115247c70424025ea2eeee',
}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def is_safe_example_email(value):
    domain = value.rsplit('@', 1)[-1].lower()
    return domain == 'users.noreply.github.com' or domain.endswith(('.example', '.invalid'))


def findings_for(path, content, exceptions=()):
    """Scan one object; exceptions match exact path, rule and matched bytes."""
    text = content.decode('utf-8', errors='replace')
    accepted = {(entry['path'], entry['rule'], entry['match_sha256']) for entry in exceptions}
    findings = []
    for name, pattern in RULES.items():
        for match in pattern.finditer(text):
            if name == 'email' and is_safe_example_email(match.group()):
                continue
            fingerprint = digest(match.group().encode())
            if (path, name, fingerprint) in accepted:
                continue
            findings.append({'path': path, 'rule': name, 'line': text[:match.start()].count('\n') + 1})
    return findings


# Exact previously reviewed image bytes remain valid only while scanning Git history.
# Current worktree files must always match REVIEWED_BINARY above.
REVIEWED_BINARY_HISTORY = {
    'docs/images/blogger-lab-paper.jpg': {'ce56c02971932a4841d73bb576e06a80b2a5af8d1c99c83e13d233f50b950a8e'},
    'docs/images/blogger-lab-scan.jpg': {'ae26d99593eca59f4aea51ece9a341919283f93d585d7b6d29488ffbcd9526b7'},
    'docs/images/blogger-lab-strategy.jpg': {'b3336384ac8d893273550211fa6baff8e3580af8b7acf2acd171d4415e9b79c0'},
    'docs/images/blogger-lab-knowledge.jpg': {'0947c81b796ba26da4eb45d30f7751aa5ac32e4a256653992117fbabc5eb6c1f'},
    'docs/images/ui-v3-overview-nav.jpg': {'948874eb59b635bd2d268f22fcaa42b499b3e78f3cf0f9930d4de0c2c2720646', '16dd7f15cd99aa2cd0c82625b1c7413cd3ff562ac2da060080ef9103aa0c859b', '87d1fac84cc019b47ae80e835da6b07231056872b50f61b46a958e287bab9ad3'},
    'docs/images/ui-v3-news-nav.jpg': {'c33b4d6f22d8bb0e440a8d2ff5c72e8a6a7a116b890eba083f4591bdb061d9c7', '73060cd3df0416e17076cc1bc39a584155551c2bf7e149a99f45f4a9cf08a98c', 'd09473314d8ed0b0e09ada7699914f2350c2b04b8c866af9750c06a9609bea21'},
    'docs/images/ui-v3-picks-nav.jpg': {'a3c27ef72cfeaec3ecc1d96e87ef4aa56346b50b8b35a2135d0c3a30777f86b1', '5f398af282f4417206d5bbe0e24d86458cd908754b1d9a9103700919eff1fde7', '8a611f50c9eb56dcb3fdedf6fc7f79fd053949db7d46f7e0d4cdaf6b38a20cf9'},
    'docs/images/ui-v3-overview.jpg': {'948874eb59b635bd2d268f22fcaa42b499b3e78f3cf0f9930d4de0c2c2720646', '16dd7f15cd99aa2cd0c82625b1c7413cd3ff562ac2da060080ef9103aa0c859b', '33e45575fad0fc7ee9bd748cac666d23aa2690c78777dea8e7ec6e9043d238b2'},
    'docs/images/ui-v3-news.jpg': {'c33b4d6f22d8bb0e440a8d2ff5c72e8a6a7a116b890eba083f4591bdb061d9c7', 'a374d090746d967cb9ad2775d42835b38b0ec3e33061e5ce4b1fe2fd9f078052', 'd09473314d8ed0b0e09ada7699914f2350c2b04b8c866af9750c06a9609bea21'},
    'docs/images/ui-v3-picks.jpg': {'a3c27ef72cfeaec3ecc1d96e87ef4aa56346b50b8b35a2135d0c3a30777f86b1', '5f398af282f4417206d5bbe0e24d86458cd908754b1d9a9103700919eff1fde7', '91a9e799e10a2f7dcda50252f8f989604cdd2261c1ebec01ce75a273b937c821'},
}


def path_findings(path, content, *, historical=False):
    parts = Path(path).parts
    issues = []
    if not parts or parts[0] in PRIVATE_ROOTS or Path(path).name in PRIVATE_NAMES:
        issues.append({'path': path, 'rule': 'private-path'})
    if Path(path).name.startswith('.env') and path != '.env.example':
        issues.append({'path': path, 'rule': 'private-environment-file'})
    if len(content) > 10 * 1024 * 1024:
        issues.append({'path': path, 'rule': 'unreviewed-large-file'})
    if path in REVIEWED_BINARY:
        allowed = {REVIEWED_BINARY[path]}
        if historical:
            allowed.update(REVIEWED_BINARY_HISTORY.get(path, ()))
        if digest(content) not in allowed:
            issues.append({'path': path, 'rule': 'binary-changed-requires-review'})
    elif Path(path).suffix not in TEXT_SUFFIXES and path not in {'.gitignore', '.gitattributes', 'LICENSE', 'NOTICE'}:
        issues.append({'path': path, 'rule': 'unreviewed-file-type'})
    if path == '.env.example':
        for number, line in enumerate(content.decode('utf-8').splitlines(), 1):
            if line.lstrip().startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if re.search('KEY|TOKEN|SECRET|PASSWORD|WEBHOOK|BASE_URL|LLM_MODEL', key) and value.strip():
                issues.append({'path': path, 'rule': 'nonempty-sensitive-example', 'line': number})
    return issues


def git(*args, root=ROOT):
    return subprocess.check_output(['git', '-C', str(root), *args])


def read_git_objects(root=ROOT):
    """Read every stored object, including unreachable objects; never use log only."""
    proc = subprocess.Popen(['git', '-C', str(root), 'cat-file', '--batch-all-objects', '--batch'], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    while True:
        header = proc.stdout.readline()
        if not header:
            break
        oid, kind, length = header.decode('ascii').strip().split()
        content = proc.stdout.read(int(length))
        if proc.stdout.read(1) != b'\n':
            raise ValueError('GIT_OBJECT_STREAM_INVALID')
        yield oid, kind, content
    if proc.wait() != 0:
        raise ValueError('GIT_OBJECT_READ_FAILED')


def load_exceptions():
    path = ROOT / '.public-privacy-fixtures.json'
    return json.loads(path.read_text(encoding='utf-8'))['fixtures']


def check_worktree():
    exceptions = load_exceptions()
    if (ROOT / '.git').exists():
        names = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode('utf-8').split('\0')
        names = sorted(set(name for name in names if name))
    else:
        names = sorted(path.relative_to(ROOT).as_posix() for path in ROOT.rglob('*') if path.is_file() and '.git' not in path.relative_to(ROOT).parts)
    issues = []
    for name in names:
        path = ROOT / name
        if path.is_symlink():
            issues.append({'path': name, 'rule': 'file-link-not-publishable'})
            continue
        content = path.read_bytes()
        issues.extend(path_findings(name, content))
        issues.extend(findings_for(name, content, exceptions))
    return {'mode': 'worktree', 'files_scanned': len(names), 'findings': issues}


def check_git():
    exceptions = load_exceptions()
    names_by_oid = {}
    commits = git('rev-list', '--all').decode().splitlines()
    paths_checked = set()
    issues = []
    for commit in commits:
        for entry in git('ls-tree', '-rz', commit).split(b'\0'):
            if not entry:
                continue
            meta, path = entry.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            path = path.decode('utf-8')
            if mode != '100644' or kind != 'blob':
                issues.append({'path': path, 'rule': 'unreviewed-git-tree-mode'})
            names_by_oid.setdefault(oid, set()).add(path)
    counts = {}
    for oid, kind, content in read_git_objects():
        counts[kind] = counts.get(kind, 0) + 1
        if kind == 'blob':
            names = names_by_oid.get(oid, {'UNREACHABLE_BLOB:' + oid})
            for path in names:
                paths_checked.add(path)
                if not path.startswith('UNREACHABLE_BLOB:'):
                    issues.extend(path_findings(path, content, historical=True))
                issues.extend(findings_for(path, content, exceptions))
        elif kind in {'commit', 'tag'}:
            issues.extend(findings_for('GIT_' + kind.upper() + ':' + oid, content))
    return {'mode': 'git', 'commits_scanned': len(commits), 'object_counts': counts, 'historical_blob_paths_scanned': len(paths_checked), 'findings': issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git', action='store_true', help='also scan all stored Git objects and all historical trees')
    args = parser.parse_args()
    reports = [check_worktree()]
    if args.git:
        reports.append(check_git())
    total = sum(len(report['findings']) for report in reports)
    print(json.dumps({'status': 'PASS' if total == 0 else 'BLOCKED', 'reports': reports}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if total else 0)


if __name__ == '__main__':
    main()
