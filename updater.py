# -*- coding: utf-8 -*-
"""Оновлення з GitHub (кнопка «Оновити»). Працює через git.
Особисте (config.json, логи) не в git і не чіпається. Локальні зміни файлів програми
перед оновленням копіюються в .update-backup/."""
import os, re, json, shutil, subprocess, time

DEFAULT_REPO = 'https://github.com/LikeJuggger/Tupa_Zelena_Vlada.git'
BRANCH = 'main'
NOWIN = {'creationflags': 0x08000000} if os.name == 'nt' else {}


class Updater(object):
    def __init__(self, root, log=print):
        self.root = root
        self.log = log

    # ---------------------------------------------------------------- helpers
    def repo_url(self):
        try:
            with open(os.path.join(self.root, 'config.json'), encoding='utf8') as f:
                return (json.load(f).get('repo') or DEFAULT_REPO).strip()
        except Exception:
            return DEFAULT_REPO

    def git_exe(self):
        p = shutil.which('git')
        if p:
            return p
        for c in (r'C:\Program Files\Git\cmd\git.exe', r'C:\Program Files (x86)\Git\cmd\git.exe',
                  '/usr/bin/git', '/opt/homebrew/bin/git', '/usr/local/bin/git'):
            if os.path.isfile(c):
                return c
        return None

    def git(self, args, timeout=120, interactive=False):
        exe = self.git_exe()
        if not exe:
            return False, '', 'no git'
        env = dict(os.environ)
        if not interactive:
            env['GIT_TERMINAL_PROMPT'] = '0'
        cmd = [exe, '-c', 'user.name=Tupa_Zelena_Vlada', '-c', 'user.email=app@local', '-c', 'core.quotepath=off'] + args
        try:
            r = subprocess.run(cmd, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout, env=env, **NOWIN)
            return r.returncode == 0, r.stdout.decode('utf8', 'ignore').strip(), r.stderr.decode('utf8', 'ignore').strip()
        except subprocess.TimeoutExpired:
            return False, '', 'timed out'
        except Exception as e:
            return False, '', str(e)

    def is_repo(self):
        return os.path.isdir(os.path.join(self.root, '.git'))

    @staticmethod
    def human(err):
        if re.search(r'Authentication failed|could not read Username|403|terminal prompts disabled', err, re.I):
            return 'GitHub не пустив — репозиторій приватний, потрібен вхід у GitHub'
        if re.search(r'Could not resolve host|unable to access|timed out', err, re.I):
            return "Немає зв'язку з GitHub — перевір інтернет"
        if re.search(r'not found|does not appear to be a git repository|Repository not found', err, re.I):
            return 'Репозиторій оновлень не знайдено'
        if err == 'no git':
            return 'Не встановлено Git — запусти INSTALL ще раз'
        lines = [l for l in err.splitlines() if l.strip()]
        return lines[-1] if lines else 'невідома помилка'

    # ---------------------------------------------------------------- api
    def version(self):
        if self.is_repo():
            ok, h, _ = self.git(['rev-parse', '--short', 'HEAD'], 10)
            ok2, d, _ = self.git(['log', '-1', '--format=%cd', '--date=format:%d.%m.%Y'], 10)
            if ok:
                return '%s · %s' % (d, h) if ok2 else h
        try:
            with open(os.path.join(self.root, 'VERSION'), encoding='utf8') as f:
                return f.read().strip()
        except Exception:
            return '—'

    def check(self):
        if not self.git_exe():
            return {'ok': False, 'error': self.human('no git')}
        url = self.repo_url()
        if not self.is_repo():
            ok, out, err = self.git(['ls-remote', url, BRANCH], 60, True)
            if not ok:
                return {'ok': False, 'error': self.human(err)}
            if not out:
                return {'ok': False, 'error': 'У репозиторії ще немає версій'}
            return {'ok': True, 'behind': 1, 'firstTime': True,
                    'changes': ['Перше підключення до GitHub — підтягну актуальну версію']}
        self.git(['remote', 'set-url', 'origin', url], 10)
        ok, _, err = self.git(['fetch', 'origin', BRANCH], 120, True)
        if not ok:
            return {'ok': False, 'error': self.human(err)}
        _, n, _ = self.git(['rev-list', '--count', 'HEAD..origin/' + BRANCH], 10)
        behind = int(n or 0)
        changes = []
        if behind:
            _, lg, _ = self.git(['log', '--format=%s', '-n', '20', 'HEAD..origin/' + BRANCH], 10)
            changes = [l for l in lg.splitlines() if l.strip()]
        _, d, _ = self.git(['log', '-1', '--format=%cd', '--date=format:%d.%m.%Y %H:%M', 'origin/' + BRANCH], 10)
        return {'ok': True, 'behind': behind, 'changes': changes, 'date': d}

    def _backup(self, files):
        if not files:
            return None
        d = os.path.join(self.root, '.update-backup', time.strftime('%Y-%m-%d_%H-%M-%S'))
        for f in files:
            src = os.path.join(self.root, f)
            if os.path.isfile(src):
                dst = os.path.join(d, f)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        return d

    def apply(self):
        if not self.git_exe():
            return {'ok': False, 'error': self.human('no git')}
        url = self.repo_url()
        fresh = not self.is_repo()
        if fresh:
            self.git(['init'], 20)
            self.git(['remote', 'add', 'origin', url], 10)
        else:
            self.git(['remote', 'set-url', 'origin', url], 10)
        ok, _, err = self.git(['fetch', 'origin', BRANCH], 300, True)
        if not ok:
            if fresh:
                shutil.rmtree(os.path.join(self.root, '.git'), ignore_errors=True)
            return {'ok': False, 'error': self.human(err)}
        okb, before, _ = self.git(['rev-parse', 'HEAD'], 10)
        before = before if okb else ''
        if fresh:
            # файли, що відрізняються від GitHub, — у бекап
            self.git(['reset', 'origin/' + BRANCH], 30)
        _, st, _ = self.git(['status', '--porcelain', '--untracked-files=no'], 20)
        changed = [l[3:].strip('"') for l in st.splitlines() if l.strip()]
        backup = self._backup(changed)
        self.git(['checkout', '-B', BRANCH, 'origin/' + BRANCH], 30)
        ok, _, err = self.git(['reset', '--hard', 'origin/' + BRANCH], 60)
        if not ok:
            return {'ok': False, 'error': self.human(err)}
        self.git(['branch', '--set-upstream-to', 'origin/' + BRANCH], 10)
        _, after, _ = self.git(['rev-parse', 'HEAD'], 10)
        self.log('оновлення: %s → %s%s' % ((before or 'нова')[:7], after[:7], ', бекап: ' + backup if backup else ''))
        return {'ok': True, 'changed': before != after,
                'backup': os.path.relpath(backup, self.root) if backup else None}
