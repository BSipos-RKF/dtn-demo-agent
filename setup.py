import os
from setuptools import setup, find_packages

setup(
    # Basic info
    name='ietf-dtn-tcpcl-demo',
    version='0.0',
    author='Brian Sipos',
    author_email='bsipos@rkf-eng.com',
    url='https://github.com/BSipos-RKF/dtn-bpbis-tcpcl',
    description='A demonstration agent for the DTN TCPCLv4.',
    long_description='''\
This implements all of the required behavior of the TCPCLv4 specification as
well as some proof-of-concept behaviors such as segment/transfer pipelining 
and dynamic segment sizing.
''',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU Lesser General Public License (LGPL)',
        'Operating System :: POSIX',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries',
    ],

    # Packages and depencies
    package_dir={
        '': 'src',
    },
    packages=find_packages(where='src'),
    install_requires=[
        'cbor2 >=4.1,<4.2',
        'crcmod >=1.7',
        'cryptography >=0.9',
        'certvalidator',
        'dbus-python',
        'portion >=2.1',
        'psutil',
        'PyGObject >=3.34', # glib integration
        'PyYAML',
        'python-mbedtls',
        'python3-dtls',
        'scapy >=2.4',
        'six',
    ],
    extras_require={},

    # Data files
    package_data={},

    # Scripts
    entry_points={
        'console_scripts': [
            'tcpcl-messagegen = tcpcl.test.messagegen:main',
            'tcpcl-bundlegen = tcpcl.test.bundlegen:main',
            'tcpcl-agent = tcpcl.cmd:main',
            'udpcl-agent = udpcl.cmd:main',
            'bp-agent = bp.cmd:main',
        ],
    },

    # Other configurations
    zip_safe=True,
    platforms='any',
)
