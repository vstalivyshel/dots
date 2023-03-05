#! /usr/bin/env python3

import functools
import itertools
import kak_socket
import os
import queue
import re
import select
import shutil
import signal
import sys
import textwrap
import threading
import time
import traceback


class KakException(Exception):
    pass


def _gen_read_cmd(cmd, cmds):
    print('def -hidden -override pk_read_%s %%{' % cmd)
    for i, cmd in enumerate(cmds):
        print('try %{' if i == 0 else '} catch %{')
        print('pk_read_' + cmd)
        if i != len(cmds) - 1:
            print('pk_done')
    print('} }')


def _gen_read_cmds():
    N = 9
    B = 4
    for i in range(1, N):
        _gen_read_cmd(str(B**i), [str(B**(i - 1)) for _ in range(B)])
    _gen_read_cmd('inf', [str(B**i) for i in range(N)] + ['inf'])


def _write(response):
    with open(_py2kak, 'w') as f:
        f.write(response)


def _read():
    with open(next(_kak2py), 'r') as f:
        dtype = f.read(1)
        if dtype == "'":
            dtype = f.read(1)
            f.read(2)
            data = unquote(f.read())
        else:
            data = f.read()
    return (dtype, data)


@functools.lru_cache(maxsize=128)
def _compile_code(code):
    code = textwrap.indent(code, ' ')
    code = '\n'.join(['def _wrapper(args):', code, '_wrapper(args)'])
    return compile(code, '<string>', 'exec')


def _process_request(request):
    try:
        args = request
        exec(_compile_code(args.pop()))
    except KeyboardInterrupt:
        raise
    except Exception:
        exc = traceback.format_exc()
        keval('\n'.join([
            'echo -markup "{Error}{\\}pykak error: '
            'see *debug* buffer"',
            "echo -debug 'pykak error:' %s" % quote(exc)
        ]))
    finally:
        _write('alias global pk_done nop')


def _getter(prefix, quoted):
    def getter_impl(name):
        return keval(('pk_write_quoted d %%%s{%s}' if quoted else
                      'pk_write "d%%%s{%s}"') % (prefix, name))[0]
    return getter_impl


def _async_worker():
    while True:
        request = _async_queue.get()
        kak_socket.send_cmd(request)


def _kak_exit_waiter():
    pid = int(os.environ['KAK_PID'])

    if hasattr(os, 'pidfd_open'):
        fd = os.pidfd_open(pid)
        select.select([fd], [], [])
    elif hasattr(select, 'kqueue'):
        kq = select.kqueue()
        kq.control([select.kevent(pid, select.KQ_FILTER_PROC,
                   select.KQ_EV_ADD, select.KQ_NOTE_EXIT)], 0)
        select.select([kq.fileno()], [], [])
    else:
        HEARTBEAT_INTERVAL_S = 60
        global _heartbeat_received
        while True:
            _heartbeat_received = False
            keval_async('pk_write h')
            time.sleep(HEARTBEAT_INTERVAL_S)
            if not _heartbeat_received:
                break

    os.kill(os.getpid(), signal.SIGINT)


def keval_async(cmd, client=None):
    if client:
        cmd = 'eval -client %s %s' % (client, quote(cmd))
    _async_queue.put(cmd)


def keval(response):
    assert threading.current_thread() == threading.main_thread()
    _write(response)
    replies = []
    while True:
        dtype, data = _read()
        if dtype == 'a':
            return replies
        elif dtype == 'd':
            replies.append(data)
        elif dtype == 'r':
            _process_request(data)
        elif dtype == 'e':
            raise KakException(data)
        else:
            raise Exception('invalid reply type "%s"' % dtype)


def unquote(s):
    return [quoted.replace("''", "'")
            for quoted in _quoted_pattern.findall(s)]


def quote(v):
    def quote_impl(s):
        return "'%s'" % s.replace("'", "''")
    if type(v) == str:
        return quote_impl(v)
    return ' '.join(quote_impl(s) for s in v)


def main():
    kak_socket.init(os.environ['kak_session'])
    pid = os.fork()
    if pid:
        _gen_read_cmds()
        print('def -hidden -override pk_read_impl %{')
        print('eval %%file{%s} }' % _py2kak)
        print('decl -hidden str pk_dir ' + quote(_pk_dir))
        print('decl -hidden bool kak2py_state true')
        print('hook -group pykak global KakEnd .* pk_stop')
        print('set global pk_running true')
        return 0
    with open('/dev/null', 'w+') as f:
        for fd in range(3):
            os.dup2(f.fileno(), fd)

    write_thread = threading.Thread(target=_async_worker)
    write_thread.daemon = True
    write_thread.start()

    exit_thread = threading.Thread(target=_kak_exit_waiter)
    exit_thread.daemon = True
    exit_thread.start()

    try:
        while True:
            dtype, data = _read()
            if dtype == 'r':
                _process_request(data)
            elif dtype == 'f':
                break
            elif dtype == 'h':
                global _heartbeat_received
                _heartbeat_received = True
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(_pk_dir)


_pk_dir = os.environ['PYKAK_DIR']
_kak2py_a = os.path.join(_pk_dir, 'kak2py_a.fifo')
_kak2py_b = os.path.join(_pk_dir, 'kak2py_b.fifo')
_kak2py = itertools.cycle((_kak2py_a, _kak2py_b))
_py2kak = os.path.join(_pk_dir, 'py2kak.fifo')
_quoted_pattern = re.compile(r"(?s)(?:'')|(?:'(.+?)(?<!')'(?!'))")
_async_queue = queue.Queue()
_heartbeat_received = False

opt = _getter('opt', False)
reg = _getter('reg', False)
val = _getter('val', False)
optq = _getter('opt', True)
regq = _getter('reg', True)
valq = _getter('val', True)
k = keval
ka = keval_async

if __name__ == '__main__':
    sys.exit(main())
