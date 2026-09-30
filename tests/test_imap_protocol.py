"""Real imap-tools over TCP; server rejects every command before DONE."""
import socketserver
import threading
import time

from imap_tools import MailBoxUnencrypted
from galvanize.config import Trigger
from galvanize.sources.imap import ImapWatcher


def test_idle_done_precedes_search_fetch_and_idle_restarts():
    commands, violations = [], []
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'* OK strict IMAP test server\r\n')
            idle_tag = None
            idle_count = 0
            while True:
                line = self.rfile.readline().decode().strip()
                if not line: return
                commands.append(line)
                if idle_tag:
                    if line != 'DONE':
                        violations.append(line)
                        self.wfile.write(b'* BAD must send DONE first\r\n')
                        return
                    self.wfile.write(f'{idle_tag} OK idle ended\r\n'.encode())
                    idle_tag = None
                    continue
                tag, command, *args = line.split()
                if command == 'CAPABILITY':
                    self.wfile.write(b'* CAPABILITY IMAP4rev1 IDLE\r\n')
                elif command == 'SELECT':
                    self.wfile.write(b'* 0 EXISTS\r\n* OK [UIDVALIDITY 1] valid\r\n')
                elif command == 'IDLE':
                    idle_tag = tag
                    idle_count += 1
                    self.wfile.write(b'+ idling\r\n')
                    time.sleep(0.05)  # notification arrives after IDLE continuation
                    self.wfile.write(b'* 1 EXISTS\r\n')
                    if idle_count == 2:
                        watcher.stop()
                    continue
                elif command == 'UID' and args[0] == 'SEARCH':
                    self.wfile.write(b'* SEARCH\r\n')
                elif command == 'LOGOUT':
                    self.wfile.write(b'* BYE\r\n')
                    self.wfile.write(f'{tag} OK logout\r\n'.encode())
                    return
                self.wfile.write(f'{tag} OK completed\r\n'.encode())

    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    watcher = ImapWatcher(Trigger('wire', {'host': 'localhost', 'user': 'test'}, {'kind': 'shell'}), lambda e: None)
    try:
        box = MailBoxUnencrypted('127.0.0.1', server.server_address[1], timeout=5).login('test', 'test')
        watchdog = threading.Timer(5, watcher.stop)
        watchdog.start()
        try:
            watcher._idle_forever(box)
        finally:
            watchdog.cancel()
            box.logout()
        assert not violations
        idle_positions = [i for i, x in enumerate(commands) if x.endswith(' IDLE')]
        assert len(idle_positions) == 2, commands
        assert any(' UID SEARCH ' in x for x in commands)
        for i, line in enumerate(commands):
            if ' UID SEARCH ' in line:
                assert commands[i - 1] == 'DONE'
    finally:
        watcher.stop()
        server.shutdown()
        server.server_close()
