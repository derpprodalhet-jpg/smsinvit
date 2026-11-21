"""Fresh offline storefront/Redis deployment smoke, using exact image IDs only.

Creates its own internal network, fresh named volumes and synthetic config.
Only /health and Redis inspection are exercised; everything created is removed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import time
import uuid


def docker(*args, stdin=None, check=True, timeout=90):
    result = subprocess.run(['docker', *args], input=stdin, capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode:
        # Only fresh fixture objects are involved; do not include configuration.
        raise RuntimeError('Docker fixture command failed: ' + args[0] + ': ' + result.stderr[:1000])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shop-image-id', required=True)
    parser.add_argument('--redis-image-id', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    for value in (args.shop_image_id, args.redis_image_id):
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', value):
            raise ValueError('Use a precise Docker image configuration ID')
    shop_image = json.loads(docker('image', 'inspect', args.shop_image_id).stdout)[0]
    redis_image = json.loads(docker('image', 'inspect', args.redis_image_id).stdout)[0]
    assert shop_image['Id'] == args.shop_image_id and redis_image['Id'] == args.redis_image_id
    assert shop_image['Architecture'] == redis_image['Architecture'] == 'arm64'
    prefix = 'dujiao-fixture-' + uuid.uuid4().hex[:12]
    network = prefix + '-network'
    shop = prefix + '-shop'
    redis = prefix + '-redis'
    volumes = {name: prefix + '-' + name for name in ['config', 'db', 'uploads', 'logs', 'redis-data']}
    created_volumes = []
    containers = []
    network_created = False
    report = {}
    try:
        docker('network', 'create', '--internal', network)
        network_created = True
        for volume in volumes.values():
            docker('volume', 'create', volume)
            created_volumes.append(volume)
        config = {
            'app': {'secret_key': secrets.token_hex(16), 'totp_issuer': 'OfflineFixture'},
            'server': {'host': '0.0.0.0', 'port': '8080', 'mode': 'release', 'trusted_proxies': []},
            'database': {'driver': 'sqlite', 'dsn': '/app/db/fixture.db'},
            'jwt': {'secret': secrets.token_hex(32)},
            'user_jwt': {'secret': secrets.token_hex(32)},
            'bootstrap': {'default_admin_username': '', 'default_admin_password': ''},
            'redis': {'enabled': True, 'host': 'redis', 'port': 6379, 'password': '', 'db': 0, 'prefix': 'offline-fixture'},
            'queue': {'enabled': True, 'host': 'redis', 'port': 6379, 'password': '', 'db': 1, 'concurrency': 1},
            'email': {'enabled': False}, 'telegram_auth': {'enabled': False}, 'google_auth': {'enabled': False},
            'cors': {'allowed_origins': ['https://shop-fixture.invalid'], 'allow_credentials': True},
            'log': {'dir': '/app/logs', 'filename': 'fixture.log'},
        }
        # JSON is valid YAML. Volume preparation has no host/data mount or NIC.
        mounts = sum((['--mount', 'type=volume,src=' + volumes[name] + ',dst=/fixture/' + name]
                      for name in ['config', 'db', 'uploads', 'logs']), [])
        docker('run', '--rm', '-i', '--network', 'none', '--user', '0:0', '--read-only',
               '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--security-opt', 'no-new-privileges:true',
               '--entrypoint', '/bin/sh', *mounts, args.shop_image_id, '-c',
               'umask 077; cat > /fixture/config/config.yml; chmod 600 /fixture/config/config.yml; '
               'chown 10001:10001 /fixture/config/config.yml /fixture/db /fixture/uploads /fixture/logs; '
               'chmod 755 /fixture/config', stdin=json.dumps(config))
        docker('run', '--rm', '--network', 'none', '--user', '0:0', '--read-only',
               '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--security-opt', 'no-new-privileges:true',
               '--entrypoint', '/bin/sh', '--mount', 'type=volume,src=' + volumes['redis-data'] + ',dst=/data',
               args.redis_image_id, '-c', 'chown 999:1000 /data')
        docker('run', '-d', '--name', redis, '--network', network, '--network-alias', 'redis',
               '--user', '999:1000', '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--mount', 'type=volume,src=' + volumes['redis-data'] + ',dst=/data',
               args.redis_image_id, 'redis-server', '--appendonly', 'yes', '--appendfsync', 'everysec')
        containers.append(redis)
        for _ in range(30):
            ping = docker('exec', redis, 'redis-cli', 'ping', check=False)
            if ping.returncode == 0 and ping.stdout.strip() == 'PONG':
                break
            time.sleep(.5)
        else:
            raise AssertionError('Fresh Redis did not become ready')
        mounts = ['--mount', 'type=volume,src=' + volumes['config'] + ',dst=/fixture-config,readonly']
        for name in ['db', 'uploads', 'logs']:
            mounts += ['--mount', 'type=volume,src=' + volumes[name] + ',dst=/app/' + name]
        docker('run', '-d', '--name', shop, '--network', network, '--user', '10001:10001',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--mount', 'type=volume,src=' + volumes['config'] + ',dst=/app/etc,readonly',
               *mounts, args.shop_image_id, './dujiao-next', '-mode', 'all')
        containers.append(shop)
        for _ in range(90):
            health = docker('exec', shop, 'wget', '-q', '-O', '-', 'http://127.0.0.1:8080/health', check=False)
            if health.returncode == 0:
                assert json.loads(health.stdout) == {'status': 'ok'}
                break
            time.sleep(.5)
        else:
            raise AssertionError('Fresh storefront health did not become ready')
        shop_inspect = json.loads(docker('inspect', shop).stdout)[0]
        redis_inspect = json.loads(docker('inspect', redis).stdout)[0]
        internal = json.loads(docker('network', 'inspect', network).stdout)[0]
        assert internal['Internal'] and set(internal['Containers']) == {shop_inspect['Id'], redis_inspect['Id']}
        app_ip = shop_inspect['NetworkSettings']['Networks'][network]['IPAddress']
        queue_keys = []
        app_connections = []
        for _ in range(30):
            clients = docker('exec', redis, 'redis-cli', 'CLIENT', 'LIST').stdout.splitlines()
            app_connections = [line for line in clients if ('addr=' + app_ip + ':') in line]
            queue_keys = docker('exec', redis, 'redis-cli', '-n', '1', '--scan', '--pattern', 'asynq:*').stdout.splitlines()
            if app_connections and any('asynq:servers' in key for key in queue_keys):
                break
            time.sleep(.5)
        assert app_connections and any('asynq:servers' in key for key in queue_keys), 'No real queue heartbeat in Redis'
        security = {}
        for name, instance, expected in [('shop', shop_inspect, '10001:10001'), ('redis', redis_inspect, '999:1000')]:
            assert instance['Config']['User'] == expected
            assert instance['HostConfig']['CapDrop'] == ['ALL']
            assert 'no-new-privileges:true' in instance['HostConfig']['SecurityOpt']
            assert not instance['HostConfig']['CapAdd'] and not instance['HostConfig']['Privileged']
            assert not instance['NetworkSettings']['Ports'] or all(value is None for value in instance['NetworkSettings']['Ports'].values())
            assert all(mount['Type'] == 'volume' for mount in instance['Mounts'])
            status = docker('exec', shop if name == 'shop' else redis, '/bin/sh', '-c', 'cat /proc/1/status').stdout
            fields = {line.split(':', 1)[0]: line.split(':', 1)[1].strip() for line in status.splitlines() if line.startswith(('Cap', 'NoNewPrivs:', 'Uid:', 'Gid:'))}
            assert all(int(fields[k], 16) == 0 for k in ['CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb'])
            assert fields['NoNewPrivs'] == '1'
            security[name] = {'user': expected, 'capabilitiesZero': True, 'noNewPrivs': True,
                              'publishedPorts': 0, 'readOnlyRoot': instance['HostConfig']['ReadonlyRootfs']}
        assert redis_inspect['HostConfig']['ReadonlyRootfs']
        assert docker('exec', shop, '/bin/sh', '-c', 'test -s /app/db/fixture.db').returncode == 0
        report = {
            'status': 'PASS', 'checkedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'scope': 'Exact ARM64 storefront and Redis images, fresh offline internal network and volumes',
            'imageConfigIds': {'shop': args.shop_image_id, 'redis': args.redis_image_id},
            'architecture': 'arm64', 'health': {'httpStatus': 200, 'json': {'status': 'ok'}},
            'freshSqliteInitialized': True, 'redisPing': 'PONG', 'queueHeartbeatInRedis': True,
            'redisAppConnections': len(app_connections), 'asynqQueueKeyCount': len(queue_keys),
            'runtimeSecurity': security, 'networkInternal': True, 'networkOtherContainers': 0,
            'namedVolumesFresh': len(created_volumes), 'hostDataMounts': 0,
            'configAndKeysSynthetic': True, 'originalProjectsReadOrMounted': False,
            'realUpstreamBusinessRequests': 0,
            'probeSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'limitations': ['Tests startup/health and real queue connection only; no customer, payment, email or supplier requests are made.',
                            'This is a local ARM64 image test; production server deployment and real provider availability remain separate checks.'],
        }
    finally:
        for container in reversed(containers):
            docker('stop', '--time', '15', container, check=False, timeout=25)
            docker('rm', '-f', container, check=False)
        if network_created:
            docker('network', 'rm', network, check=False)
        for volume in reversed(created_volumes):
            docker('volume', 'rm', volume, check=False)
    if report:
        existing_containers = docker('ps', '-a', '--format', '{{.Names}}').stdout.splitlines()
        existing_volumes = docker('volume', 'ls', '--format', '{{.Name}}').stdout.splitlines()
        existing_networks = docker('network', 'ls', '--format', '{{.Name}}').stdout.splitlines()
        assert not set(containers) & set(existing_containers)
        assert not set(created_volumes) & set(existing_volumes) and network not in existing_networks
        report['cleanupVerified'] = True
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
