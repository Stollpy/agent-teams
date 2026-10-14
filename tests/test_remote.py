from support import Fixture, module
from types import SimpleNamespace
from unittest.mock import patch
import contextlib, io, json, os, subprocess
import remote

TARGET = {'host': 'sky-a', 'path': '~/work/my project'}
LOCAL = ('.teams/teams.json', 'teams/teams.json', 'teams.json')


class Target(Fixture):
    def test_parse_target_needs_host_and_path(self):
        self.assertEqual(remote.parse_target('sky-a:~/work/p'), {'host': 'sky-a', 'path': '~/work/p'})
        self.assertEqual(remote.parse_target('me@10.0.0.2:/srv/p:x'), {'host': 'me@10.0.0.2', 'path': '/srv/p:x'})
        for bad in ('sky-a', 'sky-a:', ':~/p', ''):
            with self.assertRaises(ValueError):
                remote.parse_target(bad)

    def test_find_prefers_the_environment_then_the_nearest_file(self):
        project = self.root.resolve() / 'proj'
        (project / 'a' / 'b').mkdir(parents=True)
        (project / '.teams').mkdir()
        (project / '.teams' / 'remote.json').write_text(json.dumps({'host': 'sky-a', 'path': '~/p', 'mouse': False}))
        with patch.dict(os.environ, {'TEAMS_REMOTE': ''}):
            found = remote.find(project / 'a' / 'b', LOCAL)
        self.assertEqual(found, {'host': 'sky-a', 'path': '~/p', 'mouse': False, '_file': project / '.teams' / 'remote.json'})
        with patch.dict(os.environ, {'TEAMS_REMOTE': 'other:/srv/x'}):
            self.assertEqual(remote.find(project / 'a', LOCAL), {'host': 'other', 'path': '/srv/x'})

    def test_a_nearer_local_instance_wins_and_a_bad_file_is_refused(self):
        project = self.root.resolve() / 'proj'
        (project / 'app' / '.teams').mkdir(parents=True)
        (project / '.teams').mkdir()
        (project / '.teams' / 'remote.json').write_text(json.dumps({'host': 'sky-a', 'path': '~/p'}))
        (project / 'app' / '.teams' / 'teams.json').write_text('{}')
        with patch.dict(os.environ, {'TEAMS_REMOTE': ''}):
            self.assertIsNone(remote.find(project / 'app', LOCAL))
            (project / '.teams' / 'remote.json').write_text(json.dumps({'host': 'sky-a'}))
            with self.assertRaises(ValueError):
                remote.find(project, LOCAL)

    def test_remote_json_beside_a_local_instance_wins(self):
        inst = self.root.resolve() / 'clone' / '.teams'
        inst.mkdir(parents=True)
        (inst / 'teams.json').write_text('{}')
        (inst / 'remote.json').write_text(json.dumps({'host': 'sky-a', 'path': '~/p'}))
        with patch.dict(os.environ, {'TEAMS_REMOTE': ''}):
            self.assertEqual(remote.find(inst.parent, LOCAL)['host'], 'sky-a')


class Relay(Fixture):
    def test_remote_line_runs_teams_in_the_project_through_a_login_shell(self):
        home = self.root.resolve()
        (home / 'work' / 'my project').mkdir(parents=True)
        target = {'host': 'h', 'path': '~/work/my project', 'command': 'pwd && printf "%s\\n"'}
        line = remote.remote_line(target, ['a b', "it's", '$HOME'])
        self.assertTrue(line.startswith('exec "$SHELL" -lc '), line)
        r = subprocess.run(['/bin/sh', '-c', line], capture_output=True, text=True, timeout=10,
                           env={'SHELL': '/bin/sh', 'HOME': str(home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')})
        self.assertEqual(r.stdout.splitlines(), [str(home / 'work' / 'my project'), 'a b', "it's", '$HOME'])
        self.assertIn('&& teams ', remote.remote_line(TARGET, ['status']))       # the default command

    def test_ssh_argv_places_options_before_the_host(self):
        target = {**TARGET, 'ssh_options': ['-p', '2222']}
        self.assertEqual(remote.ssh_argv(target, 'cmd', tty=True, options=['-N']), ['ssh', '-t', '-N', '-p', '2222', 'sky-a', 'cmd'])
        self.assertEqual(remote.ssh_argv(TARGET), ['ssh', 'sky-a'])

    def test_run_streams_and_returns_the_remote_exit_code(self):
        with patch.object(remote.subprocess, 'call', return_value=3) as call:
            self.assertEqual(remote.run(TARGET, ['down']), 3)
        self.assertEqual(call.call_args.args[0], ['ssh', 'sky-a', remote.remote_line(TARGET, ['down'])])
        with patch.object(remote.subprocess, 'call', side_effect=OSError('no ssh')):
            with self.assertRaises(remote.RemoteError):
                remote.run(TARGET, ['down'])

    def test_relay_hands_the_process_to_ssh_with_a_terminal_only_when_interactive(self):
        for interactive, flag in ((True, ['-t']), (False, [])):
            with patch.object(remote, '_interactive', return_value=interactive), patch.object(remote.os, 'execvp') as execvp:
                remote.relay(TARGET, ['task', 'list'])
            self.assertEqual(execvp.call_args.args, ('ssh', ['ssh', *flag, 'sky-a', remote.remote_line(TARGET, ['task', 'list'])]))

    def test_main_relays_a_command(self):
        with patch.object(remote, 'relay') as relay:
            remote.main(TARGET, SimpleNamespace(cmd='task'), ['task', 'list'])
        relay.assert_called_once_with(TARGET, ['task', 'list'])

    def test_quote_never_leaves_a_word_bare(self):
        self.assertEqual(remote.quote('status'), "'status'")
        self.assertEqual(remote.quote('=ls'), "'=ls'")
        self.assertEqual(remote.quote(''), "''")
        self.assertEqual(remote.quote("it's"), "'it'\"'\"'s'")
        line = remote.remote_line({'host': 'h', 'path': '~/my project'}, ['msg', 'web', '=ls'])
        self.assertEqual(line, 'exec "$SHELL" -lc ' + remote.quote("cd ~/'my project' && teams 'msg' 'web' '=ls'"))


HOSTILE = ['=ls', '==', '=', '=nosuchcmd_zz', '%self', '~', '~root', '*', '?', '$HOME', '`id`', '$(id)', 'a b', "it's", '', '-n', '--',
           '{a,b}', '<1-3>', '!!', '#x', 'a\\b', '\\', 'é', 'a\nb', ';', '|', '&', '>', 'x=y', '[a]', 'a\'b"c', '\t', '=(ls)', '<(ls)']


class Shells(Fixture):
    """The relay line read by real shells acting as the server's login shell: every word arrives as typed."""
    def setUp(self):
        super().setUp()
        self.home = self.root.resolve() / 'home'                  # the remote user's home: no start-up file in it
        for name in ('proj', '=ls', '=x y', '-P'):
            (self.home / name).mkdir(parents=True)
        self.there = self.root.resolve() / 'teams-there'          # stands for `teams` on the server: says where it runs and what it received
        self.there.write_text('#!/bin/sh\nprintf "teams-there\\n"; pwd\nfor word; do printf "%s\\0" "$word"; done\n')
        self.there.chmod(0o755)

    def arrive(self, shell, path, words):
        """The relay line as ssh hands it to `shell`: (the directory the command ran in, the words it received)."""
        target = {'host': 'h', 'path': path, 'command': "'" + str(self.there) + "'"}
        env = {'SHELL': shell, 'HOME': str(self.home), 'ZDOTDIR': str(self.home), 'ENV': '', 'PATH': '/usr/bin:/bin'}
        r = subprocess.run([shell, '-c', remote.remote_line(target, words)], capture_output=True, text=True, timeout=30, env=env, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        cwd, _, received = r.stdout.rpartition('teams-there\n')[2].partition('\n')   # a login shell may print before the command does
        return cwd, received.split('\0')[:-1]

    def verbatim(self, name):
        shell = next((p for p in (f'/bin/{name}', f'/usr/bin/{name}', f'/usr/local/bin/{name}', f'/opt/homebrew/bin/{name}') if os.path.exists(p)), None)
        if not shell:
            self.skipTest(f'{name} is not installed here')
        self.assertEqual(self.arrive(shell, str(self.home / 'proj'), ['msg', *HOSTILE])[1], ['msg', *HOSTILE])
        for path, there in (('=ls', self.home / '=ls'), ('~/=x y', self.home / '=x y'), ('-P', self.home / '-P'), ('~', self.home),
                            ('~/proj', self.home / 'proj')):
            with self.subTest(path=path):
                self.assertEqual(self.arrive(shell, path, ['status']), (str(there), ['status']))

    def test_sh(self):
        self.verbatim('sh')

    def test_bash(self):
        self.verbatim('bash')

    def test_zsh(self):
        self.verbatim('zsh')

    def test_dash(self):
        self.verbatim('dash')

    def test_ksh(self):
        self.verbatim('ksh')


class Status(Fixture):
    def answer(self, stdout='', stderr='', code=0):
        return patch.object(remote.subprocess, 'run', return_value=SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr))

    def test_status_reads_the_last_json_line(self):
        with self.answer('Welcome to sky-a\n{"up": false, "teams": []}\n') as run:
            self.assertEqual(remote.status(TARGET), {'up': False, 'teams': []})
        self.assertEqual(run.call_args.args[0], ['ssh', 'sky-a', remote.remote_line(TARGET, ['status', '--json'])])

    def test_status_reads_a_line_holding_unicode_line_separators(self):
        purpose = 'first' + chr(0x2028) + 'second' + chr(0x2029) + 'third' + chr(0x85) + 'end'   # str.splitlines() cuts at each of them
        answer = {'up': True, 'teams': [{'name': 'web', 'purpose': purpose}]}
        with self.answer('Welcome to sky-a\n' + json.dumps(answer, ensure_ascii=False) + '\n'):
            self.assertEqual(remote.status(TARGET), answer)                    # one line of JSON is read as one line

    def test_status_failures_name_the_target_and_hint_an_old_engine(self):
        with self.answer(stderr='teams: no teams.json found here or above\n', code=1):
            with self.assertRaisesRegex(remote.RemoteError, r'^sky-a:~/work/my project: teams: no teams.json'):
                remote.status(TARGET)
        with self.answer(stderr='usage: teams …\nteams: error: unrecognized arguments: --json\n', code=2):
            with self.assertRaisesRegex(remote.RemoteError, 'update it with git pull'):
                remote.status(TARGET)
        with self.answer(stdout='no json here\n'):
            with self.assertRaises(remote.RemoteError):
                remote.status(TARGET)
        with patch.object(remote.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 60)):
            with self.assertRaises(remote.RemoteError):
                remote.status(TARGET)


class Link(Fixture):
    def test_link_checks_the_server_before_writing(self):
        inst = self.root.resolve() / 'here' / '.teams'
        with patch.object(remote, 'status', side_effect=remote.RemoteError('sky-a:~/p: unreachable')):
            with self.assertRaises(remote.RemoteError):
                remote.link('sky-a:~/p', str(inst))
        self.assertFalse((inst / 'remote.json').exists())
        st = {'up': True, 'engine': '0.2.0', 'teams': [{'name': 'manager'}, {'name': 'web'}]}
        out = io.StringIO()
        with patch.object(remote, 'status', return_value=st), contextlib.redirect_stdout(out):
            self.assertEqual(remote.link('sky-a:~/p', str(inst)), 0)
        self.assertEqual(json.loads((inst / 'remote.json').read_text()), {'host': 'sky-a', 'path': '~/p'})
        self.assertIn('manager, web', out.getvalue())
        self.assertNotIn('(was ', out.getvalue())
        out = io.StringIO()
        with patch.object(remote, 'status', return_value=st), contextlib.redirect_stdout(out):
            remote.link('other:/srv/p', str(inst))
        self.assertIn('(was sky-a:~/p)', out.getvalue())
        self.assertEqual(json.loads((inst / 'remote.json').read_text()), {'host': 'other', 'path': '/srv/p'})

    def test_strip_option_removes_both_spellings(self):
        self.assertEqual(remote.strip_option(['--remote', 'h:p', 'task', 'list'], '--remote'), ['task', 'list'])
        self.assertEqual(remote.strip_option(['--remote=h:p', 'status'], '--remote'), ['status'])
        self.assertEqual(remote.strip_option(['msg', 'web', '--remote-ish'], '--remote'), ['msg', 'web', '--remote-ish'])
        # only ahead of the command: from the command on, the words are its own and travel untouched
        self.assertEqual(remote.strip_option(['--remote=a:p', '--remote', 'b:p', 'msg', 'web', '--', '--remote', 'next', 'word'], '--remote'),
                         ['msg', 'web', '--', '--remote', 'next', 'word'])
        self.assertEqual(remote.strip_option(['msg', 'web', '--remote=is broken'], '--remote'), ['msg', 'web', '--remote=is broken'])
        self.assertEqual(remote.strip_option(['--remote', 'h:p', '--', 'status'], '--remote'), ['--', 'status'])


class Dispatch(Fixture):
    """bin/teams main(): which commands go remote, with which arguments."""
    def setUp(self):
        super().setUp()
        self.launcher = module('launcher_remote_' + self._testMethodName, 'teams')
        self.project = self.root.resolve() / 'proj'
        (self.project / 'sub').mkdir(parents=True)
        (self.project / '.teams').mkdir()
        (self.project / '.teams' / 'remote.json').write_text(json.dumps({'host': 'sky-a', 'path': '~/p'}))

    def dispatch(self, argv, cwd=None, env=None):
        """main() with argv; the (target, argv) handed to remote.main, or None when the command ran here."""
        launcher, seen = self.launcher, []
        def fake_main(target, a, rest):
            seen.append((target, rest))
            return 0
        environ = {'TEAMS_CONFIG': '', 'TEAMS_REMOTE': '', **(env or {})}
        with patch.object(launcher.sys, 'argv', ['teams', *argv]), patch.dict(os.environ, environ), \
             patch.object(launcher.Path, 'cwd', return_value=cwd or self.project / 'sub'), \
             patch.object(launcher.remote, 'main', side_effect=fake_main), \
             patch.object(launcher, 'load', side_effect=RuntimeError('ran here')), \
             patch.object(launcher, 'cmd_init', side_effect=RuntimeError('ran here')):
            try:
                launcher.main()
            except SystemExit:
                if seen:
                    return seen[0]
                raise
            except RuntimeError:
                return None
        return seen[0] if seen else None

    def test_a_remote_json_above_the_cwd_sends_the_command_there(self):
        target, rest = self.dispatch(['task', 'list'])
        self.assertEqual((target['host'], target['path']), ('sky-a', '~/p'))
        self.assertEqual(rest, ['task', 'list'])

    def test_the_flag_wins_and_is_not_relayed(self):
        target, rest = self.dispatch(['--remote', 'other:/srv/x', 'status'])
        self.assertEqual(target, {'host': 'other', 'path': '/srv/x'})
        self.assertEqual(rest, ['status'])
        _, rest = self.dispatch(['--remote', 'other:/srv/x', 'msg', 'web', '--', '--remote', 'next', 'word'])
        self.assertEqual(rest, ['msg', 'web', '--', '--remote', 'next', 'word'])  # the message keeps its words
        # one spelling only: an abbreviation would reach the server unstripped and be relayed again from there
        for short in (['--r', 'other:/srv/x'], ['--remot', 'other:/srv/x'], ['--rem=other:/srv/x']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.dispatch([*short, 'status'])
            self.assertEqual(caught.exception.code, 2, short)

    def test_config_and_teams_config_keep_the_instance_local(self):
        self.assertIsNone(self.dispatch(['--config', str(self.root / 'teams.json'), 'status']))
        self.assertIsNone(self.dispatch(['status'], env={'TEAMS_CONFIG': str(self.root / 'teams.json')}))

    def test_teams_remote_applies_but_init_never_walks(self):
        target, _ = self.dispatch(['status'], cwd=self.root.resolve(), env={'TEAMS_REMOTE': 'env-host:/srv/y'})
        self.assertEqual(target['host'], 'env-host')
        self.assertIsNone(self.dispatch(['init', '--dry-run']))                  # a remote.json above does not capture init
        _, rest = self.dispatch(['--remote', 'h:/p', 'init', '--dry-run'])       # the flag does
        self.assertEqual(rest, ['init', '--dry-run'])

    def test_remote_and_config_together_are_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.dispatch(['--remote', 'h:/p', '--config', str(self.root / 'teams.json'), 'status'])
        self.assertEqual(caught.exception.code, 1)

    def test_link_is_handled_here(self):
        with patch.object(self.launcher.sys, 'argv', ['teams', 'link', 'sky-a:~/p', '--dir', 'x']), \
             patch.object(self.launcher.remote, 'link', return_value=0) as link:
            self.launcher.main()
        link.assert_called_once_with('sky-a:~/p', 'x')
