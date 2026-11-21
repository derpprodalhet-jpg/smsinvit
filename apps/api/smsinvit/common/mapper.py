"""Frozen SQL from legacy MyBatis annotations, using bound parameters only.

A deliberately small interpreter supports the original mapper <if>/<foreach>
syntax. Neither query strings nor identifiers come from arbitrary client input.
"""
import html
import json
import re
from importlib.resources import files
from xml.etree import ElementTree as ET

CATALOG=json.loads(files(__package__).joinpath('mapper_sql.json').read_text())

class Mapper:
    def __init__(self,db,name): self.db,self.methods=db,CATALOG[name]
    def __getattr__(self,name):
        spec=self.methods[name]
        def call(*args,**kwargs):
            values=dict(zip(spec['params'],args));values.update(kwargs)
            sql,params=render(spec['sql'],values)
            if spec['operation']!='Select': return self.db.execute(sql,*params).rowcount
            ret=spec['return']
            if ret.startswith('List<'):
                rows=self.db.all(sql,*params)
                return rows if 'Map<' in ret else [next(iter(row.values())) for row in rows]
            row=self.db.one(sql,*params)
            if 'Map<' in ret: return row
            value=next(iter(row.values())) if row else None
            if ret=='boolean': return bool(value)
            if ret in ('long','int'): return int(value or 0)
            return value
        return call

def condition(expression,values):
    # Every expression is static from checked-in Java mapper source. Parse only
    # comparisons / conjunctions, without eval or user-controlled expressions.
    parts=re.split(r'\s+or\s+',expression)
    for disj in parts:
        allowed=True
        for atom in re.split(r'\s+and\s+',disj):
            atom=atom.strip()
            m=re.fullmatch(r"([A-Za-z_]\w*)(?:\.size\(\))?\s*(==|!=|>=|<=|>|<)\s*(null|''|\d+|true|false)",atom)
            if not m: raise ValueError('Unsupported static SQL condition')
            field,op,rhs=m.groups();left=values.get(field)
            if '.size()' in atom:left=len(left or [])
            right=None if rhs=='null' else '' if rhs=="''" else True if rhs=='true' else False if rhs=='false' else int(rhs)
            result=left==right if op=='==' else left!=right if op=='!=' else left>=right if op=='>=' else left<=right if op=='<=' else left>right if op=='>' else left<right
            allowed=allowed and result
        if allowed:return True
    return False

def render(sql,values):
    params=[]
    def text(source,local):
        if not source:return ''
        def bind(m):
            key=m.group(1).split(',')[0];value=local
            for part in key.split('.'):
                value=value.get(part) if isinstance(value,dict) else getattr(value,part)
            params.append(value);return '?'
        return re.sub(r'#\{([^}]+)\}',bind,source)
    def visit(node,local):
        out=text(node.text,local)
        for child in node:
            if child.tag=='if':
                if condition(child.attrib['test'],local): out+=visit(child,local)
            elif child.tag=='foreach':
                items=[]
                for value in local.get(child.attrib['collection'],[]) or []:
                    scope=dict(local);scope[child.attrib.get('item','item')]=value;items.append(visit(child,scope))
                if items:out+=child.attrib.get('open','')+child.attrib.get('separator','').join(items)+child.attrib.get('close','')
            elif child.tag=='where':
                body=visit(child,local).strip();body=re.sub(r'^(AND|OR)\s+','',body,flags=re.I)
                if body:out+=' WHERE '+body
            else:raise ValueError('Unsupported static SQL tag')
            out+=text(child.tail,local)
        return out
    if '<script>' in sql: return visit(ET.fromstring(sql),values),params
    return text(html.unescape(sql),values),params
