# setup.py

import os
from urllib import request, parse
from distutils.core import setup
from distutils.extension import Extension
from Cython.Build import cythonize

# only use safe raw_input with Python-2, input is considered harmful
try:
    from past.builtins import raw_input  # try python-future
except ImportError:
    try:
        raw_input()  # Python-2
    except NameError:
        raw_input = input  # Python-3

DIRNAME = os.path.dirname(__file__)
SPA_C_URL = r'https://midcdmz.nrel.gov/apps/download.pl'
SPA_H_URL = r'https://midcdmz.nrel.gov/spa/spa.h'
VALUES = {'software': 'SPA'}
LICENSE = 'SPA_NOTICE.md'

with open(os.path.join(DIRNAME, LICENSE)) as f:
    print(f.read())

print('\nEnter the following information to accept the NREL LICENSE ...\n')
VALUES['name'] = raw_input('Name: ')
VALUES['company'] = raw_input('Company (or enter "Individual"): ')
VALUES['country'] = raw_input('Country: ')
VALUES['email'] = raw_input('Email (optional): ')
DATA = parse.urlencode(VALUES).encode('ascii')

# get spa.c
REQ = request.Request(SPA_C_URL, DATA)
with request.urlopen(REQ) as response:
    SPA_C = response.read()
# replace timezone with time_zone to avoid a nameclash the function
# __timezone which is defined by a MACRO in pyconfig.h as timezone
SPA_C = SPA_C.replace(b'timezone', b'time_zone')
with open(os.path.join(DIRNAME, 'spa.c'), 'wb') as f:
    f.write(SPA_C)

# get spa.h
with request.urlopen(SPA_H_URL) as response:
    SPA_H = response.read()
# replace timezone with time_zone to avoid a nameclash the function
# __timezone which is defined by a MACRO in pyconfig.h as timezone
SPA_H = SPA_H.replace(b'timezone', b'time_zone')
with open(os.path.join(DIRNAME, 'spa.h'), 'wb') as f:
    f.write(SPA_H)

SPA_SOURCES = [os.path.join(DIRNAME, src) for src in ['spa_py.pyx', 'spa.c']]

setup(
    ext_modules=cythonize([Extension('spa_py', SPA_SOURCES)])
)
