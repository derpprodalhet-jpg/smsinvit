#!/usr/bin/env python3
"""Prepare a new private server directory; retained database encryption keys are explicit."""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta,timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from database_tools import MaintenanceError

def private_write(path,data,mode=0o600,uid=None):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
    with os.fdopen(fd,'wb') as stream:stream.write(data if isinstance(data,bytes) else data.encode())
    if uid is not None and os.geteuid()==0:os.chown(path,uid,uid)

def prepare(destination,domain,existing_secrets=None,new_install=False):
    destination=Path(destination).resolve()
    if destination.exists():raise MaintenanceError('Private deployment directory must be new; existing credentials are never replaced.')
    if any(parent.name=='codex_invit' for parent in (destination,*destination.parents)):raise MaintenanceError('The protected original invitation project may not be used as a deployment destination.')
    if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',domain) or '.' not in domain:raise MaintenanceError('Supply the production hostname without a URL or port.')
    if new_install==bool(existing_secrets):raise MaintenanceError('Select exactly one: retained private secrets file or explicit new installation.')
    if existing_secrets:
        path=Path(existing_secrets)
        if path.stat().st_mode&0o077:raise MaintenanceError('Retained secrets file must have owner-only 0600 permissions.')
        keys=json.loads(path.read_text())
        expected=['inventory_encryption_key','inventory_fingerprint_key','jwt_secret']
        if any(not isinstance(keys.get(key),str) or not keys[key] for key in expected):raise MaintenanceError('Retained encryption, fingerprint and JWT keys are all required.')
        if not re.fullmatch('[0-9A-Fa-f]{64}',keys['inventory_encryption_key']):raise MaintenanceError('Retained encryption key must be the unchanged 32-byte hex key.')
    else:keys={name:secrets.token_hex(32) for name in ['inventory_encryption_key','inventory_fingerprint_key','jwt_secret']}
    destination.mkdir(parents=True,mode=0o700);(destination/'init').mkdir(mode=0o750);tls=destination/'mysql-tls';tls.mkdir(mode=0o750)
    if os.geteuid()==0:
        os.chown(tls,999,999);os.chown(destination/'init',999,999)
    now=datetime.now(timezone.utc);ca_key=rsa.generate_private_key(public_exponent=65537,key_size=3072);server_key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
    ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'SmsInvit private database CA')]);server_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'mysql')])
    ca=x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=3650)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(x509.KeyUsage(digital_signature=True,key_encipherment=False,content_commitment=False,data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,encipher_only=False,decipher_only=False),critical=True).sign(ca_key,hashes.SHA256())
    server=x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name).public_key(server_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=730)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.SubjectAlternativeName([x509.DNSName('mysql'),x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).sign(ca_key,hashes.SHA256())
    private_write(destination/'mysql-ca-key.pem',ca_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    private_write(tls/'ca.pem',ca.public_bytes(serialization.Encoding.PEM),mode=0o644)
    private_write(tls/'server.pem',server.public_bytes(serialization.Encoding.PEM),mode=0o644,uid=999)
    private_write(tls/'server-key.pem',server_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()),mode=0o400,uid=999)
    passwords={name:secrets.token_urlsafe(48) for name in ['root','runtime','migration','backup']};private_write(destination/'mysql-root-password',passwords['root']+'\n',mode=0o400,uid=999)
    grants={'runtime':'SELECT,INSERT,UPDATE,DELETE','migration':'SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES','backup':'SELECT,SHOW VIEW,TRIGGER,EVENT'}
    lines=[]
    for kind,permissions in grants.items():
        user='smsinvit_'+kind;lines += [f"CREATE USER '{user}'@'%' IDENTIFIED BY '{passwords[kind]}' REQUIRE SSL;",f"GRANT {permissions} ON `smsinvit`.* TO '{user}'@'%';"]
    private_write(destination/'init/001-accounts.sql','\n'.join(lines)+'\n',mode=0o640,uid=999)
    settings={'MYSQL_HOST':'mysql','MYSQL_PORT':3306,'MYSQL_DATABASE':'smsinvit','MYSQL_USER':'smsinvit_runtime','MYSQL_PASSWORD':passwords['runtime'],'MYSQL_TLS_MODE':'VERIFY_IDENTITY','MYSQL_SSL_CA':'/run/secrets/mysql-ca.pem','smsinvit':{'inventory-encryption-key':keys['inventory_encryption_key'],'inventory-fingerprint-key':keys['inventory_fingerprint_key'],'jwt-secret':keys['jwt_secret'],'worker-enabled':False,'security':{'trusted-proxies':'172.31.77.10/32'}},'runtime':{'public_api_base':'https://'+domain,'cors_origins':'https://'+domain,'secure_cookies':True,'security_hsts_enabled':True}}
    private_write(destination/'runtime.yaml',yaml.safe_dump(settings,sort_keys=False),uid=10001)
    for kind in ['migration','backup']:
        private_write(destination/(kind+'.json'),json.dumps({'host':'mysql','port':3306,'user':'smsinvit_'+kind,'password':passwords[kind],'database':'smsinvit','tls_mode':'verify-identity','ca_file':'/run/secrets/mysql-ca.pem'},indent=2)+'\n',uid=10001)
    private_write(destination/'retained-secrets.json',json.dumps({name:keys[name] for name in ['inventory_encryption_key','inventory_fingerprint_key','jwt_secret']},indent=2)+'\n')
    return {'status':'prepared','directory':str(destination),'databaseTLS':'certificate_and_hostname_verified','workersEnabled':False,'retainedEncryptionKeys':bool(existing_secrets),'ownershipPrepared':os.geteuid()==0}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--domain',required=True);mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--retained-secrets-file',type=Path);mode.add_argument('--new-install',action='store_true');args=parser.parse_args()
    try:print(json.dumps(prepare(args.directory,args.domain,args.retained_secrets_file,args.new_install)))
    except (MaintenanceError,ValueError) as error:parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()
