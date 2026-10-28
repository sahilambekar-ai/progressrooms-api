"""Exercise deployment phase failures using fake Docker/curl, never a live host."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('failure',['backup','migration','api','workers','ready','none'])
def test_deployment_failure_preserves_release_and_stops_unsafe_writers(tmp_path,failure):
    source=Path(__file__).parents[1]/'deploy'
    release='a'*40
    root=tmp_path/'host';bundle=root/'incoming'/f'api-{release}';bundle.mkdir(parents=True)
    for name in ['api.env','postgres.env','.env','compose.yaml']:(root/name).write_text('# synthetic\n')
    (root/'api-image.env').write_text('API_IMAGE_TAG=previous\n')
    for name in ['compose.api.yaml','backup-db.sh','setup-uat-validation.sh']:shutil.copyfile(source/name,bundle/name)
    (bundle/'api-image.tar.gz').write_bytes(b'synthetic-image')
    binaries=tmp_path/'bin';binaries.mkdir();events=tmp_path/'events'
    docker=binaries/'docker';docker.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
with open(os.environ['EVENTS'],'a') as f:f.write(json.dumps(args)+'\\n')
failure=os.environ['FAILURE']
phase=''
if 'exec' in args and 'sh' in args:phase='backup'
elif 'run' in args and 'alembic' in args:phase='migration'
elif 'up' in args and args[-1]=='api':phase='api'
elif 'up' in args and args[-1]=='meeting-email-worker':phase='workers'
if phase and phase==failure:sys.exit(9)
if phase=='backup':sys.stdout.buffer.write(b'synthetic-archive')
''');docker.chmod(0o700)
    curl=binaries/'curl';curl.write_text('#!/bin/sh\n[ "$FAILURE" != ready ]\n');curl.chmod(0o700)
    # macOS lacks flock; adapter takes the isolated test's uncontended lock.
    flock=binaries/'flock';flock.write_text('#!/bin/sh\nexit 0\n');flock.chmod(0o700)
    env={**os.environ,'PATH':str(binaries)+os.pathsep+os.environ['PATH'],'DEPLOY_ROOT':str(root),'EVENTS':str(events),'FAILURE':failure}
    result=subprocess.run(['bash',str(source/'uat.sh'),release],env=env,capture_output=True,text=True)
    calls=[json.loads(line) for line in events.read_text().splitlines()]
    migrations=[a for a in calls if 'run' in a and 'alembic' in a]
    stops=[a for a in calls if 'stop' in a]
    if failure=='none':
        assert result.returncode==0,result.stderr
        assert (root/'api-image.env').read_text()==f'API_IMAGE_TAG={release}\n'
        assert not (bundle/'api-image.tar.gz').exists()
    else:
        assert result.returncode!=0
        assert (root/'api-image.env').read_text()=='API_IMAGE_TAG=previous\n'
        assert (bundle/'api-image.tar.gz').exists()
        assert bool(stops)==(failure in {'api','workers','ready'})
    if failure=='backup':assert not migrations
    if failure in {'backup','migration'}:assert not any('up' in a and a[-1]=='api' for a in calls)
    if failure!='backup':assert list((root/'backups').glob('*/database.dump.sha256'))
