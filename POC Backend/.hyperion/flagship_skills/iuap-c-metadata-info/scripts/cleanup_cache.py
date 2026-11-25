#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层缓存清理脚本 - Python 执行器"""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

PY_INDEX_PATH = os.environ.get('PY_INDEX_PATH', '')
PY_BILLNAME = os.environ.get('PY_BILLNAME', '')
PY_URI = os.environ.get('PY_URI', '')
PY_SAFE_NAME = os.environ.get('PY_SAFE_NAME', '')
PY_CACHE_FILE = os.environ.get('PY_CACHE_FILE', '')
PY_TIMESTAMP = os.environ.get('PY_TIMESTAMP', '')
PY_SESSION_ID = os.environ.get('PY_SESSION_ID', '')


def action_check():
    if os.path.exists(PY_INDEX_PATH):
        with open(PY_INDEX_PATH, encoding='utf-8') as f:
            d = json.load(f)
        e = d.get('global', {}).get('index', {}).get(PY_BILLNAME, {})
        print('INDEX_HIT:' + e.get('file', '') if e else 'NOT_FOUND')
    else:
        print('NOT_FOUND')


def action_precise_delete():
    if os.path.exists(PY_INDEX_PATH):
        with open(PY_INDEX_PATH, encoding='utf-8') as f:
            d = json.load(f)
        e = d.get('global', {}).get('index', {}).get(PY_BILLNAME, {})
        if e:
            fp = e.get('absolutePath', '')
            if fp and os.path.exists(fp):
                os.remove(fp)
                print('[删除] ' + fp)
            del d['global']['index'][PY_BILLNAME]
            print('[删除] 全局索引: ' + PY_BILLNAME)
        for s in d.get('sessions', {}).values():
            s.get('localIndex', {}).pop(PY_BILLNAME, None)
        with open(PY_INDEX_PATH, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print('[完成]')


def action_register():
    if os.path.exists(PY_INDEX_PATH):
        with open(PY_INDEX_PATH, encoding='utf-8') as f:
            d = json.load(f)
    else:
        d = {'version': 3, 'global': {'index': {}}, 'sessions': {}}
    d.setdefault('global', {}).setdefault('index', {})[PY_BILLNAME] = {
        'uri': PY_URI,
        'file': PY_SAFE_NAME + '.txt',
        'absolutePath': PY_CACHE_FILE,
        'created': PY_TIMESTAMP,
        'version': 1
    }
    d.setdefault('sessions', {}).setdefault(PY_SESSION_ID, {}).setdefault('localIndex', {})[PY_BILLNAME] = PY_SAFE_NAME + '.txt'
    with open(PY_INDEX_PATH, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    # 创建缓存文件
    os.makedirs(os.path.dirname(PY_CACHE_FILE), exist_ok=True)
    with open(PY_CACHE_FILE, 'w', encoding='utf-8') as f:
        f.write(f'<!-- billname: {PY_BILLNAME} -->\n')
        f.write(f'<!-- uri: {PY_URI} -->\n')
        f.write(f'<!-- session: {PY_SESSION_ID} -->\n')
        f.write(f'<!-- created: {PY_TIMESTAMP} -->\n')
    print('REGISTERED:' + PY_SAFE_NAME + '.txt')


def action_list_all():
    if os.path.exists(PY_INDEX_PATH):
        with open(PY_INDEX_PATH, encoding='utf-8') as f:
            d = json.load(f)
        entries = d.get('global', {}).get('index', {})
        if not entries:
            print('(空)')
        else:
            for n, i in entries.items():
                print('  ' + n)
                print('    URI: ' + i.get('uri', '-'))
                print('    File: ' + i.get('file', '-'))
                print('    Created: ' + i.get('created', '-'))
                print()
    else:
        print('(空)')


if __name__ == '__main__':
    action = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--action':
            action = args[i + 1] if i + 1 < len(args) else None
            break
        i += 1

    if action == 'check':
        action_check()
    elif action == 'precise-delete':
        action_precise_delete()
    elif action == 'register':
        action_register()
    elif action == 'list-all':
        action_list_all()
    else:
        print('未知操作: ' + str(action), file=sys.stderr)
