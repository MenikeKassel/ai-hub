"""Run offline KOL regression tests without opening the live runtime."""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source = root / '_automation' / 'trading_research'
    sys.path[:0] = [str(source), str(source / 'tests')]
    output = root / '_runtime' / 'test-results'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kol-offline-tests-') as isolated:
        os.environ['KOL_OFFLINE_TESTS'] = '1'
        os.environ['TRADING_RUNTIME_ROOT'] = str(Path(isolated) / 'runtime')
        os.environ['ASHARE_FOUNDATION_ROOT'] = str(Path(isolated) / 'foundation')
        os.environ['KOL_DISCOVERY_CAPTURE_ROOT'] = str(Path(isolated) / 'captures')
        def protect_external_state(event, args):
            if event == 'socket.connect':
                # AnyIO/asyncio uses a loopback socketpair to coordinate its
                # in-process test portal.  Permit that local transport while
                # still rejecting connections to real hosts.
                address = args[1] if len(args) > 1 else None
                host = address[0] if isinstance(address, tuple) and address else ''
                if host not in {'127.0.0.1', '::1', 'localhost'}:
                    raise AssertionError('Offline tests cannot connect to external services')
            if event == 'subprocess.Popen':
                command = str(args[0]).lower()
                if any(name in command for name in ('schtasks', 'hermes', 'twitter', 'codex.exe', 'codex.cmd')) and not command.endswith('python.exe'):
                    raise AssertionError('Offline tests cannot launch operator or provider processes')
        sys.addaudithook(protect_external_state)
        suite = (unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]) if sys.argv[1:]
                 else unittest.defaultTestLoader.discover(str(source / 'tests'), 'test_*.py'))
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        (output / 'kol-console-tests.log').write_text(stream.getvalue(), encoding='utf-8')
        print(f'Tests: {result.testsRun}; failures: {len(result.failures)}; errors: {len(result.errors)}; skipped: {len(result.skipped)}')
        for test, detail in (result.failures + result.errors)[:8]:
            print(test.id())
            print('\n'.join(detail.splitlines()[-8:]))
        print(f'Log: {output / "kol-console-tests.log"}')
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
