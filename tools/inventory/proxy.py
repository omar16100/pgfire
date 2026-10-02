#!/usr/bin/env python3
"""Local PostgreSQL v3 capture proxy. Records SQL/format metadata, never passwords/values.

Intended only for synthetic local test databases. SQL text itself may contain literals.
SSL/GSS negotiation is refused so both subsequent directions remain inspectable.
"""
import argparse
import asyncio
import itertools
import json
import struct
from pathlib import Path

MAX_FRAME = 64 * 1024 * 1024


def u32(data, offset=0):
    return struct.unpack_from('!I', data, offset)[0]


def cstring(data, offset=0):
    end = data.index(0, offset)
    return data[offset:end].decode('utf-8', errors='replace'), end + 1


def describe(direction, kind, body):
    result = {'direction': direction, 'kind': kind, 'bytes': len(body)}
    if direction == 'client':
        if kind == 'Q':
            result['sql'], _ = cstring(body)
        elif kind == 'P':
            result['statement'], offset = cstring(body)
            result['sql'], offset = cstring(body, offset)
            count = struct.unpack_from('!H', body, offset)[0]
            result['parameter_oids'] = list(struct.unpack_from(f'!{count}I', body, offset + 2))
        elif kind == 'B':
            result['portal'], offset = cstring(body)
            result['statement'], offset = cstring(body, offset)
            count = struct.unpack_from('!H', body, offset)[0]
            offset += 2
            result['parameter_formats'] = list(struct.unpack_from(f'!{count}H', body, offset))
            offset += 2 * count
            count = struct.unpack_from('!H', body, offset)[0]
            offset += 2
            result['parameter_count'] = count
            for _ in range(count):
                size = struct.unpack_from('!i', body, offset)[0]
                if size < -1:
                    raise ValueError('invalid Bind parameter length')
                offset += 4 + max(0, size)
                if offset > len(body):
                    raise ValueError('truncated Bind parameter')
            count = struct.unpack_from('!H', body, offset)[0]
            result['result_formats'] = list(struct.unpack_from(f'!{count}H', body, offset + 2))
        elif kind == 'E':
            result['portal'], offset = cstring(body)
            result['max_rows'] = u32(body, offset)
        elif kind in ('D', 'C'):
            result['object_kind'] = chr(body[0])
            result['name'], _ = cstring(body, 1)
    elif kind == 'E':
        offset = 0
        while offset < len(body) and body[offset]:
            field = chr(body[offset])
            value, offset = cstring(body, offset + 1)
            if field in ('C', 'S'):
                result[{'C': 'sqlstate', 'S': 'severity'}[field]] = value
    elif kind == 'Z' and body:
        result['transaction_status'] = chr(body[0])
    return result


async def read_frame(reader, startup=False):
    kind = b'' if startup else await reader.readexactly(1)
    header = await reader.readexactly(4)
    length = u32(header)
    if length < 4 or length > MAX_FRAME:
        raise ValueError(f'invalid PostgreSQL frame length {length}')
    body = await reader.readexactly(length - 4)
    return kind, body, kind + header + body


async def serve(args):
    sequence = itertools.count(1)
    connections = itertools.count(1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', buffering=1) as output:
        def record(connection, event):
            output.write(json.dumps({'seq': next(sequence), 'connection': connection, **event}) + '\n')

        async def connected(client, client_writer):
            connection = next(connections)
            upstream_writer = None
            tasks = []
            try:
                while True:
                    _, body, packet = await read_frame(client, startup=True)
                    code = u32(body)
                    if code in (80877103, 80877104):
                        client_writer.write(b'N')
                        await client_writer.drain()
                        continue
                    break
                upstream, upstream_writer = await asyncio.open_connection(args.upstream_host, args.upstream_port)
                upstream_writer.write(packet)
                await upstream_writer.drain()
                record(connection, {'direction': 'client', 'kind': 'startup', 'protocol': code})
                if code == 80877102:
                    return

                async def pump(reader, writer, direction):
                    while True:
                        kind, body, packet = await read_frame(reader)
                        event = describe(direction, kind.decode('ascii'), body)
                        record(connection, event)
                        writer.write(packet)
                        await writer.drain()

                tasks = [asyncio.create_task(pump(client, upstream_writer, 'client')),
                         asyncio.create_task(pump(upstream, client_writer, 'server'))]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            except (ValueError, IndexError, struct.error, UnicodeError) as error:
                record(connection, {'kind': 'capture_error', 'error': str(error)})
            finally:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for writer in (client_writer, upstream_writer):
                    if writer:
                        writer.close()
                        try:
                            await writer.wait_closed()
                        except ConnectionError:
                            pass

        server = await asyncio.start_server(connected, args.listen_host, args.listen_port)
        print(f'capture proxy listening on {args.listen_host}:{args.listen_port}', flush=True)
        async with server:
            await server.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen-host', default='127.0.0.1')
    parser.add_argument('--listen-port', type=int, default=55433)
    parser.add_argument('--upstream-host', default='127.0.0.1')
    parser.add_argument('--upstream-port', type=int, default=55432)
    parser.add_argument('--output', type=Path, required=True)
    try:
        asyncio.run(serve(parser.parse_args()))
    except KeyboardInterrupt:
        pass
