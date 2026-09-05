import builtins
import os
import plistlib
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from caty_gateway import cli


def test_help_lists_all_six_subcommands_with_descriptions(capsys):
    assert cli.main([]) == 0
    text = capsys.readouterr().out
    for command in ('setup', 'status', 'serve', 'qr', 'push', 'doctor'):
        assert command in text
    normalized = ' '.join(text.split())
    for description in (
        'Install, start, verify, and pair one gateway member',
        'Passive preflight for a backend, port, and public URL',
        'Send a gateway event',
        'Reissue the pairing QR (use --member to load the installed service environment)',
    ):
        assert description in normalized


@pytest.mark.parametrize('command', ['setup', 'status', 'serve', 'qr', 'doctor'])
def test_subcommand_help(command, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, '--help'])
    assert exit_info.value.code == 0
    assert 'usage:' in capsys.readouterr().out


def test_status_delegates_all_options():
    with mock.patch('caty_gateway.setup_orchestrator.main', return_value=7) as run:
        assert cli.main(['status', '--member', 'fake-member', '--wait']) == 7
    run.assert_called_once_with(['--status', '--member', 'fake-member', '--wait'])


def test_setup_delegates_all_options():
    flags = ['--member', 'fake-member', '--backend', 'codex', '--no-history', '--yes', '--plan-only']
    with mock.patch('caty_gateway.setup_orchestrator.main', return_value=0) as run:
        assert cli.main(['setup', *flags]) == 0
    run.assert_called_once_with(flags)


def test_push_delegates_nested_parser():
    with mock.patch('caty_gateway.caty_push.main', return_value=2) as run:
        assert cli.main(['push', 'open-url', '--url', 'https://example.invalid']) == 2
    run.assert_called_once_with(['open-url', '--url', 'https://example.invalid'])


def test_qr_delegates_without_serve_guard(monkeypatch):
    monkeypatch.setenv('CATY_TOKEN', '')
    monkeypatch.setenv('CATY_GATEWAY_BIND', '0.0.0.0')
    with mock.patch('caty_gateway.caty_gateway.main', return_value=0) as run:
        assert cli.main(['qr', '--qr-delivery', 'url']) == 0
    run.assert_called_once_with(['qr', '--qr-delivery', 'url'])


def test_module_help_is_independent_of_backend_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv('CATY_BACKEND', 'hermes')
    monkeypatch.delenv('CATY_HERMES_API_KEY', raising=False)
    result = subprocess.run([sys.executable, '-B', '-m', 'caty_gateway', '--help'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert 'doctor' in result.stdout
    assert 'Traceback' not in result.stderr


def test_serve_refuses_before_runtime_import(monkeypatch, capsys):
    monkeypatch.setenv('CATY_TOKEN', ' \t ')
    monkeypatch.setenv('CATY_GATEWAY_BIND', '0.0.0.0')
    with mock.patch('caty_gateway.caty_gateway.main') as run:
        assert cli.main(['serve']) == 2
    run.assert_not_called()
    assert 'CATY_TOKEN' in capsys.readouterr().err


def test_codex_factory_uses_existing_preset(monkeypatch):
    from caty_gateway import caty_gateway as gateway
    monkeypatch.setattr(gateway, 'BACKEND_NAME', 'codex')
    with mock.patch('caty_gateway.backends.generic_cli.GenericCliBackend') as backend:
        gateway._build_backend()
    assert backend.call_args.kwargs['preset'] == 'codex'


@pytest.mark.parametrize('arguments', [[], ['serve']])
def test_legacy_module_serve_fails_closed_before_backend_config(arguments, monkeypatch, tmp_path):
    monkeypatch.setenv('CATY_GATEWAY_BIND', '0.0.0.0')
    monkeypatch.setenv('CATY_TOKEN', '')
    monkeypatch.setenv('CATY_BACKEND', 'hermes')
    monkeypatch.setenv('CATY_HERMES_API_KEY', '')
    result = subprocess.run([sys.executable, '-B', '-m', 'caty_gateway.caty_gateway', *arguments], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 2
    assert len(result.stderr.splitlines()) == 1
    assert 'CATY_TOKEN must be non-empty' in result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('command', [
    ['setup', '--member', 'fake-member', '--backend', 'fake-backend'],
    ['doctor', '--backend', 'fake-backend'],
])
def test_unknown_backend_has_release_error(command, capsys):
    assert cli.main(command) == 2
    assert capsys.readouterr().err.strip() == (
        "post-release: backend 'fake-backend' is not supported in this release; "
        "supported: claude, codex, openclaw, hermes, openai-compat"
    )


@pytest.fixture
def member_runtime(monkeypatch, tmp_path):
    # Isolate every loaded variable and never read or write the real HOME.
    monkeypatch.setattr(os, 'environ', dict(os.environ, HOME=str(tmp_path)))
    monkeypatch.setattr(cli.platform, 'system', lambda: 'Linux')
    run = mock.Mock(return_value=0)
    imported_environments = []
    original_import = builtins.__import__

    def import_runtime(name, *args, **kwargs):
        if name == 'caty_gateway.caty_gateway':
            imported_environments.append(dict(os.environ))
            return SimpleNamespace(main=run)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', import_runtime)
    return run, imported_environments


def test_qr_member_linux_loads_before_import_and_preserves_flags(tmp_path, member_runtime):
    path = tmp_path / '.config/caty-gateway/fake-member.env'
    path.parent.mkdir(parents=True)
    path.write_text(
        '# Installed environment\n'
        'CATY_TOKEN="inst-tok-1"\n'
        "CATY_ID='fake-member'\n"
        'CATY_GATEWAY_PORT=9876\n'
        'CATY_PUBLIC_URL="https://example.invalid/member"\n'
        'CATY_NAME="A \\"quoted\\" name"\n'
        "CUSTOM_VALUE='literal $value'\n"
        'PATH="/fake/service/bin"\n', encoding='utf-8',
    )
    os.environ.update(CATY_TOKEN='shell-tok-1', CATY_GATEWAY_PORT='1234')
    shell_path = os.environ.get('PATH')
    assert cli.main(['qr', '--qr-delivery', 'url', '--member', 'fake-member', '--wait-visible-seconds', '30']) == 0
    run, imports = member_runtime
    run.assert_called_once_with(['qr', '--qr-delivery', 'url', '--wait-visible-seconds', '30'])
    assert len(imports) == 1
    assert imports[0]['CATY_TOKEN'] == 'inst-tok-1'
    assert imports[0]['CATY_GATEWAY_PORT'] == '9876'
    assert imports[0]['CATY_ID'] == 'fake-member'
    assert imports[0]['CATY_NAME'] == 'A "quoted" name'
    assert imports[0]['CUSTOM_VALUE'] == 'literal $value'
    assert imports[0]['CATY_PUBLIC_URL'] == 'https://example.invalid/member'
    assert imports[0].get('PATH') == shell_path


@pytest.mark.parametrize(('line', 'expected'), [
    (br"CUSTOM_VALUE='literal\'" + b'\n', 'literal\\'),
    (br'CUSTOM_VALUE="literal\\"' + b'\n', 'literal\\'),
])
def test_qr_member_linux_accepts_valid_terminal_backslashes(tmp_path, member_runtime, line, expected):
    path = tmp_path / '.config/caty-gateway/fake-member.env'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'CATY_TOKEN=inst-tok-1\n' + line)

    assert cli.main(['qr', '--member', 'fake-member']) == 0
    run, imports = member_runtime
    run.assert_called_once_with(['qr'])
    assert imports[0]['CUSTOM_VALUE'] == expected


@pytest.mark.parametrize('member_flags', [['--member=fake-member'], ['--mem', 'fake-member']])
def test_qr_member_darwin_loads_plist(tmp_path, monkeypatch, member_runtime, member_flags):
    monkeypatch.setattr(cli.platform, 'system', lambda: 'Darwin')
    path = tmp_path / 'Library/LaunchAgents/ai.caty.gateway.fake-member.plist'
    path.parent.mkdir(parents=True)
    values = dict(CATY_TOKEN='plist-tok-1', CATY_ID='fake-member', CATY_GATEWAY_PORT='9876',
                  CATY_PUBLIC_URL='https://example.invalid/member', CUSTOM_VALUE='A "quoted" value')
    path.write_bytes(plistlib.dumps({'EnvironmentVariables': dict(values, PATH='/fake/service/bin')}))
    os.environ.update(CATY_TOKEN='shell-tok-1', CATY_GATEWAY_PORT='1234')
    shell_path = os.environ.get('PATH')
    assert cli.main(['qr', *member_flags, '--qr-delivery', 'url']) == 0
    run, imports = member_runtime
    run.assert_called_once_with(['qr', '--qr-delivery', 'url'])
    assert len(imports) == 1
    assert all(imports[0][key] == value for key, value in values.items())
    assert imports[0].get('PATH') == shell_path


@pytest.mark.parametrize('system,content', [
    ('Linux', None),
    ('Darwin', None),
    ('Linux', b'CATY_ID=fake-member\n'),
    ('Linux', b'CATY_TOKEN="   "\n'),
    ('Linux', b'CATY_TOKEN="fake-secret\n'),
    ('Linux', br'CATY_TOKEN="fake-secret\"' + b'\n'),
    ('Linux', b'CATY_TOKEN=fake-secret\ninvalid-line\n'),
    ('Linux', b'CATY_TOKEN=fake-secret\x00\n'),
    ('Linux', b'\xff'),
    ('Darwin', b'not-a-plist-fake-secret'),
    ('Darwin', b'<?xml version="1.0"?><plist><dict>'),
    ('Darwin', plistlib.dumps({'EnvironmentVariables': {'CATY_ID': 'fake-member'}})),
    ('Darwin', plistlib.dumps({'EnvironmentVariables': {'CATY_TOKEN': 123}})),
    ('Windows', None),
])
def test_qr_member_failure_is_safe_before_import(system, content, tmp_path, monkeypatch, member_runtime, capsys):
    monkeypatch.setattr(cli.platform, 'system', lambda: system)
    if content is not None:
        relative = '.config/caty-gateway/fake-member.env' if system == 'Linux' else 'Library/LaunchAgents/ai.caty.gateway.fake-member.plist'
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    os.environ['CATY_TOKEN'] = 'shell-tok-1'
    assert cli.main(['qr', '--member', 'fake-member']) == 2
    run, imports = member_runtime
    run.assert_not_called()
    assert imports == []
    output = capsys.readouterr()
    assert output.out == ''
    assert len(output.err.splitlines()) == 1
    assert 'shell-tok-1' not in output.err
    if system == 'Windows':
        assert output.err.strip() == (
            "ERROR: unsupported platform 'Windows'; "
            'caty-gateway qr --member supports Linux and macOS'
        )
    else:
        relative = (
            '.config/caty-gateway/fake-member.env'
            if system == 'Linux'
            else 'Library/LaunchAgents/ai.caty.gateway.fake-member.plist'
        )
        assert output.err.strip() == (
            "ERROR: no installed environment for member 'fake-member' at "
            f'{tmp_path / relative}; '
            'run caty-gateway setup --member fake-member first'
        )


def test_legacy_qr_member_directs_to_public_cli(capsys):
    from caty_gateway.caty_gateway import _qr_cli_args
    with pytest.raises(SystemExit) as exit_info:
        _qr_cli_args(['--member', 'fake-member'])
    assert exit_info.value.code == 2
    assert capsys.readouterr().err.splitlines() == [
        'ERROR: --member requires the public CLI: caty-gateway qr --member ID'
    ]


def test_setup_invalid_member_keeps_setup_error(tmp_path):
    from caty_gateway.setup_orchestrator import SetupError, SetupOrchestrator

    with pytest.raises(SetupError, match='--member must contain only'):
        SetupOrchestrator(['--member', '..', '--plan-only'], env={'HOME': str(tmp_path)})
