"""Regression coverage for event data, webhook wiring, and daemon discovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from galvanize import dispatch, manage, serve
from galvanize.bus import TriggerBus
from galvanize.config import Trigger, load_triggers
from galvanize.events import Event


def test_prompt_and_payload_are_literal_process_arguments(tmp_path):
    output = tmp_path / 'args.json'
    marker = tmp_path / 'should-not-exist'
    script = tmp_path / 'arguments.py'
    script.write_text('import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding="utf-8")\n')
    prompt = f'" & echo injected > "{marker}" & " $(touch nope) $HOME %PATH% {{payload}} 中文'
    event = Event('demo', 'emit', 'changed', {'title': prompt})
    command = f'"{sys.executable}" "{script}" "{output}" "{{prompt}}" "{{payload}}"'
    trigger = Trigger('demo', {'type': 'emit'}, {'kind': 'shell', 'command': command})
    ok, detail = dispatch._wake_shell(trigger, event, prompt)
    assert ok, detail
    assert json.loads(output.read_text(encoding='utf-8')) == [prompt, json.dumps(event.to_body(), ensure_ascii=False)]
    assert not marker.exists()


def test_posix_templates_split_before_event_substitution():
    trigger = Trigger('demo', {'type': 'emit'}, {'kind': 'shell', 'command': 'agent --prompt "{prompt}" --payload "{payload}"'})
    event = Event('demo', 'emit', 'changed', {'title': '$HOME; $(touch nope)'})
    prompt = '$HOME; $(touch nope) quotes " and spaces {payload}'
    with patch.object(dispatch.os, 'name', 'posix'), patch.object(dispatch.subprocess, 'run') as run:
        run.return_value = subprocess.CompletedProcess([], 0, 'ok', '')
        assert dispatch._wake_shell(trigger, event, prompt)[0]
    assert run.call_args.args[0] == ['agent', '--prompt', prompt, '--payload', json.dumps(event.to_body(), ensure_ascii=False)]
    assert run.call_args.kwargs['shell'] is False


def test_templated_shell_operators_are_rejected():
    with patch.object(dispatch.os, 'name', 'posix'):
        with pytest.raises(ValueError, match='single executable'):
            dispatch._command_args('agent "{prompt}" | another')


@pytest.mark.skipif(os.name != 'nt', reason='Windows npm shim behavior')
def test_npm_shim_runs_node_directly(tmp_path):
    shim = tmp_path / 'dsh.cmd'
    entry = tmp_path / 'node_modules' / 'dsh' / 'bin.js'
    entry.parent.mkdir(parents=True)
    entry.write_text('// npm entry')
    shim.write_text('@echo off\n"%_prog%" "%dp0%\\node_modules\\dsh\\bin.js" %*\n')
    with patch.object(dispatch.shutil, 'which', side_effect=lambda name: str(shim) if name == 'dsh' else 'node.exe'):
        assert dispatch._resolve_executable(['dsh', '--profile', 'headless', '{prompt}']) == [
            'node.exe', str(entry), '--profile', 'headless', '{prompt}']
    shim.write_text('@echo off\necho %*')
    with patch.object(dispatch.shutil, 'which', return_value=str(shim)):
        with pytest.raises(ValueError, match='Batch-file'):
            dispatch._resolve_executable(['dsh', '{prompt}'])


def test_later_file_changes_are_allowed_but_bursts_are_deduplicated():
    trigger = Trigger('t', {'type': 'folder'}, {'kind': 'shell', 'command': 'echo'}, dedupe_key='{path}')
    bus = TriggerBus()
    event = Event('t', 'folder', 'file.modified', {'path': '/a.txt'})
    with patch('galvanize.bus.dispatch_mod.dispatch', return_value=(True, 'fired')) as fire:
        with patch('galvanize.bus.time.time', return_value=1000):
            assert bus.handle(trigger, event) == (True, 'fired')
        with patch('galvanize.bus.time.time', return_value=1001):
            assert 'dedupe' in bus.handle(trigger, event)[1]
        with patch('galvanize.bus.time.time', return_value=1003):
            assert bus.handle(trigger, event) == (True, 'fired')
    assert fire.call_count == 2


def test_shell_webhook_without_receiver_is_refused():
    result = manage.add_trigger('webhook', name='dsh-webhook', wake='shell', command='dsh "{prompt}"')
    assert not result['ok']
    assert 'relay' in result['error'].lower()
    assert 'dsh-webhook' not in load_triggers()


def test_shell_webhook_with_relay_returns_ingestion_url():
    result = manage.add_trigger('webhook', name='dsh-webhook', wake='shell', command='dsh "{prompt}"',
                                relay_url='https://relay.example', relay_token='read-token')
    assert result['ok'], result
    assert result['url'] == 'https://relay.example/ingest/dsh-webhook'
    assert load_triggers()['dsh-webhook'].source['relay'] is True


def test_old_live_server_is_still_discovered():
    serve.serve_info_path().write_text(json.dumps({'port': 12345, 'pid': os.getpid(), 'ts': 1000}))
    with patch('galvanize.serve.time.time', return_value=1000 + 86400), patch('urllib.request.urlopen') as probe:
        probe.return_value.__enter__.return_value.read.return_value = b'{"api_version":1}'
        assert serve.running_info()['port'] == 12345
        assert probe.called
    with patch('urllib.request.urlopen', side_effect=OSError('not running')):
        assert serve.running_info() is None


def test_actual_relay_worker_authentication():
    import shutil
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to run the actual Cloudflare worker')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('relay_worker.test.mjs'))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
