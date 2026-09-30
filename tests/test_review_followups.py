import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from galvanize import cli, manage, mcp, serve, state
from galvanize.bus import TriggerBus
from galvanize.config import GlobalConfig, Trigger, load_triggers, upsert_trigger
from galvanize.events import Event
from galvanize.sources.relay import RelayWatcher


def event(key='001', body=None, route='test'):
    return {'id': key, 'route': route, 'body': json.dumps(body if body is not None else {'hello': 'world'})}


def test_relay_restart_dedupes_stable_ids_and_isolates_relays():
    got = []
    first = RelayWatcher('https://one.invalid/', 't', lambda r, e: got.append(e.payload['event_id']))
    first.state.stage([event()], '001')
    first.process_pending()
    second = RelayWatcher('https://one.invalid', 't', lambda r, e: got.append(e.payload['event_id']))
    assert second._since == '001'
    second.state.stage([event()], '001')  # server replays retained id
    second.process_pending()
    assert got == ['001']
    other = RelayWatcher('https://two.invalid', 't', lambda r, e: got.append(e.payload['event_id']))
    other.state.stage([event()], '001')
    other.process_pending()
    assert got == ['001', '001']


def test_relay_restart_recovers_received_but_unacknowledged_event():
    first = RelayWatcher('https://relay.invalid', 't', lambda *a: None)
    first.state.stage([event()], '001')  # crash after receipt, before dispatch
    got = []
    second = RelayWatcher(first.url, 't', lambda r, e: got.append(e.payload['event_id']))
    second.process_pending()
    assert got == ['001']
    assert second._since == '001'


def test_relay_crash_after_side_effect_before_ack_is_at_least_once(monkeypatch):
    got = []
    first = RelayWatcher('https://relay.invalid', 't', lambda r, e: got.append(e.payload['event_id']))
    first.state.stage([event()], '001')
    monkeypatch.setattr(first.state, 'finish', lambda *a, **kw: (_ for _ in ()).throw(SystemExit('crash')))
    with pytest.raises(SystemExit):
        first.process_pending()
    second = RelayWatcher(first.url, 't', lambda r, e: got.append(e.payload['event_id']))
    second.process_pending()
    assert got == ['001', '001']  # explicitly documented unavoidable acknowledgement window


def test_failed_wake_retries_boundedly_without_ack_and_does_not_block_later_items():
    attempts = []
    def emit(route, evt):
        attempts.append(evt.payload['event_id'])
        return (False, 'cannot spawn') if evt.payload['event_id'] == '001' else (True, 'done')
    w = RelayWatcher('https://relay.invalid', 't', emit)
    w.state.stage([event('001'), event('002')], '002')
    w.process_pending(now=100)
    assert w._since == '0'
    assert attempts == ['001', '002']
    w.process_pending(now=101)
    assert attempts == ['001', '002']
    for now in (200, 300, 400, 500, 600):
        w.process_pending(now=now)
    assert attempts.count('001') == 5
    assert w.state.failures()[0]['state'] == 'failed'
    assert w.state.failures()[0]['error'] == 'cannot spawn'
    gc = GlobalConfig.load(); gc.relay_url = w.url; gc.save()
    assert manage.status()['relay_failures'][0]['id'] == '001'
    assert manage.retry_relay_event('001')['ok']
    w.emit_by_route = lambda *a: (True, 'recovered')
    w.process_pending(now=700)
    assert w._since == '002'
    assert not w.state.failures()


@pytest.mark.parametrize('body', [[1, 2], 42, 'hello'])
def test_non_object_relay_payload_is_normalized(body):
    got = []
    w = RelayWatcher('https://relay.invalid', 't', lambda r, e: got.append(e.payload))
    w.state.stage([event(body=body)], '001')
    w.process_pending()
    assert got == [{'value': body, 'event_id': '001'}]
    assert w._since == '001'


def test_malformed_relay_item_is_quarantined_without_blocking_following_event():
    got = []
    w = RelayWatcher('https://relay.invalid', 't', lambda r, e: got.append(e.payload['event_id']))
    w.state.stage([event('001', route=''), event('002')], '002')
    w.process_pending()
    assert got == ['002']
    assert w.state.failures()[0]['state'] == 'quarantined'
    assert w._since == '002'


def test_mcp_requires_wake_and_forwards_all_creation_options(monkeypatch, tmp_path):
    assert 'wake' in mcp.TOOLS[0]['inputSchema']['required']
    assert not json.loads(mcp._call_tool('trigger_add', {'kind': 'emit', 'name': 'missing'}))['ok']
    saved_secrets = {}
    monkeypatch.setattr('galvanize.secrets.set_secret', lambda k, v: saved_secrets.update({k: v}))
    monkeypatch.setattr('galvanize.secrets.index_keyring_key', lambda k: None)
    out = json.loads(mcp._call_tool('trigger_add', {
        'kind': 'webhook', 'name': 'from-mcp', 'wake': 'shell', 'command': 'agent "{prompt}"',
        'workdir': str(tmp_path), 'relay_url': 'https://relay.invalid', 'relay_token': 'read-token'}))
    assert out['ok'] and out['url'] == 'https://relay.invalid/ingest/from-mcp'
    assert load_triggers()['from-mcp'].wake == {'kind': 'shell', 'command': 'agent "{prompt}"', 'workdir': str(tmp_path)}
    assert saved_secrets['relay:token'] == 'read-token'


@pytest.mark.parametrize('profile', ['headless', 'custom-wake'])
def test_cli_dsh_uses_shared_profile(profile):
    assert serve.call_op('configure_dsh_profile', {'wake_profile': profile})['ok']
    assert cli.main(['add', 'emit', '--name', 'from-cli', '--wake', 'dsh']) == 0
    assert load_triggers()['from-cli'].wake['command'] == f'dsh --profile {profile} "{{prompt}}"'
    assert cli.main(['add', 'emit', '--name', 'override', '--wake', 'dsh', '--wake-profile', 'another']) == 0
    assert load_triggers()['override'].wake['command'] == 'dsh --profile another "{prompt}"'


@pytest.mark.parametrize('outcome', ['success', 'failure', 'exception'])
def test_concurrent_duplicate_reservation_and_failure_release(outcome):
    bus = TriggerBus()
    trigger = Trigger('test', {'type': 'folder'}, {'kind': 'shell', 'command': 'unused'}, dedupe_key='{path}')
    evt = Event('test', 'folder', 'modified', {'path': 'one.txt'})
    entered, release = threading.Event(), threading.Event()
    def dispatch(*args):
        entered.set()
        assert release.wait(5)
        if outcome == 'exception':
            raise RuntimeError('injected')
        return outcome == 'success', outcome
    with patch('galvanize.bus.dispatch_mod.dispatch', side_effect=dispatch) as fn:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(bus.handle, trigger, evt)
            assert entered.wait(5)
            assert 'in-flight' in bus.handle(trigger, evt)[1]
            assert fn.call_count == 1
            release.set()
            if outcome == 'exception':
                with pytest.raises(RuntimeError): first.result()
            else:
                first.result()
    with patch('galvanize.bus.dispatch_mod.dispatch', return_value=(True, 'retry')) as fn:
        bus.handle(trigger, evt)
        assert fn.call_count == (0 if outcome == 'success' else 1)


@pytest.mark.parametrize('kind,relay', [('folder', False), ('imap', False), ('webhook', True)])
def test_watching_requires_live_daemon_and_connected_source(kind, relay):
    source = {'type': kind, **({'relay': True} if relay else {})}
    upsert_trigger(Trigger('health', source, {'kind': 'shell', 'command': 'unused'}))
    key = 'relay' if relay else f'{kind}:health'
    state.set_source_health(key, True)
    assert not manage.status()['triggers'][0]['watching']
    state.set_heartbeat()
    assert manage.status()['triggers'][0]['watching']
    state.set_source_health(key, False, 'disconnected')
    assert not manage.status()['triggers'][0]['watching']
    assert manage.status()['triggers'][0]['watcher_error'] == 'disconnected'


def test_manual_emit_is_not_a_watcher():
    upsert_trigger(Trigger('manual', {'type': 'emit'}, {'kind': 'shell', 'command': 'unused'}))
    state.set_heartbeat()
    assert not manage.status()['triggers'][0]['watching']


@pytest.mark.parametrize('detail', ['skipped: cooldown (2s < 60s)', 'skipped: dedupe in-flight key'])
def test_relay_does_not_acknowledge_a_throttled_retry(detail):
    watcher = RelayWatcher('https://relay.invalid', 't', lambda *a: (False, 'failed wake'))
    watcher.state.stage([event()], '001')
    watcher.process_pending(now=0)
    watcher.emit_by_route = lambda *a: (True, detail)
    watcher.process_pending(now=3)
    assert watcher.state.cursor() == '0'
    assert watcher.state.failures()[0]['attempts'] == 1
    watcher.emit_by_route = lambda *a: (True, 'delivered')
    watcher.process_pending(now=8)
    assert watcher.state.cursor() == '001'


def test_relay_waits_through_full_cooldown_without_exhausting_attempts(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('galvanize.bus.time.time', lambda: clock[0])
    delivered = []
    monkeypatch.setattr('galvanize.bus.dispatch_mod.dispatch',
                        lambda t, e, p: (delivered.append(e.payload['event_id']) is None, 'delivered'))
    trigger = Trigger('test', {'type': 'webhook', 'relay': True},
                      {'kind': 'shell', 'command': 'unused'}, cooldown_s=60)
    bus = TriggerBus()
    watcher = RelayWatcher('https://relay.invalid', 't', lambda r, e: bus.handle(trigger, e))
    watcher.state.stage([event('001'), event('002')], '002')
    for elapsed in range(0, 60, 2):
        clock[0] = 1000 + elapsed
        watcher.process_pending(now=clock[0])
    assert delivered == ['001']
    assert watcher.state.cursor() == '001'
    assert watcher.state.pending(now=1060)[0]['attempts'] == 0
    assert not watcher.state.failures()
    # A normal restart must preserve the deferred event and its budget.
    watcher = RelayWatcher(watcher.url, 't', lambda r, e: bus.handle(trigger, e))
    clock[0] = 1060
    watcher.process_pending(now=clock[0])
    assert delivered == ['001', '002']
    assert watcher.state.cursor() == '002'


def test_relay_waits_for_long_inflight_wake_and_keeps_real_failure_budget(monkeypatch):
    started, release = threading.Event(), threading.Event()
    def dispatch(t, e, p):
        started.set()
        assert release.wait(10)
        return False, 'cannot spawn'
    monkeypatch.setattr('galvanize.bus.dispatch_mod.dispatch', dispatch)
    trigger = Trigger('test', {'type': 'emit'}, {'kind': 'shell', 'command': 'unused'}, dedupe_key='{key}')
    bus = TriggerBus()
    watcher = RelayWatcher('https://relay.invalid', 't', lambda r, e: bus.handle(trigger, e))
    watcher.state.stage([event(body={'key': 'shared'})], '001')
    with ThreadPoolExecutor(max_workers=1) as pool:
        wake = pool.submit(bus.handle, trigger, Event('test', 'emit', 'test', {'key': 'shared'}))
        try:
            assert started.wait(5)
            for now in range(0, 62, 2):
                watcher.process_pending(now=now)
            assert watcher.state.cursor() == '0'
            assert watcher.state.pending(now=62)[0]['attempts'] == 0
        finally:
            release.set()
        assert wake.result() == (False, 'cannot spawn')
    for now in (62, 64, 68, 76, 92):
        watcher.process_pending(now=now)
    assert watcher.state.failures()[0]['state'] == 'failed'
    assert watcher.state.failures()[0]['attempts'] == 5
    assert watcher.state.retry('001')
    monkeypatch.setattr('galvanize.bus.dispatch_mod.dispatch', lambda *a: (True, 'recovered'))
    watcher.process_pending(now=94)
    assert watcher.state.cursor() == '001'


def test_deferred_batch_cannot_starve_a_later_healthy_route():
    delivered = []
    def emit(route, evt):
        if route == 'blocked':
            return True, 'skipped: cooldown (0s < 60s)'
        delivered.append(evt.payload['event_id'])
        return True, 'delivered'
    watcher = RelayWatcher('https://relay.invalid', 't', emit)
    watcher.state.stage([event(f'{i:03}', route='blocked') for i in range(100)]
                        + [event('100', route='healthy')], '100')
    watcher.process_pending(now=0)
    assert not delivered
    watcher.process_pending(now=2)
    assert delivered == ['100']
    assert watcher.state.cursor() == '0'  # Deferred items remain unacknowledged.
    for now in range(4, 30, 2):
        watcher.process_pending(now=now)
    assert delivered == ['100']
    assert not watcher.state.failures()
