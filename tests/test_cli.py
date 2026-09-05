import subprocess
import sys
from unittest import mock

import pytest

from caty_gateway import cli


def test_no_arguments_print_help(capsys):
    assert cli.main([]) == 0
    text = capsys.readouterr().out
    for command in ('setup', 'status', 'serve', 'qr', 'push', 'doctor'):
        assert command in text


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
    result = subprocess.run([sys.executable, '-m', 'caty_gateway', '--help'], cwd=tmp_path, capture_output=True, text=True)
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
    result = subprocess.run([sys.executable, '-m', 'caty_gateway.caty_gateway', *arguments], cwd=tmp_path, capture_output=True, text=True)
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
