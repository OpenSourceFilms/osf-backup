#!/usr/bin/env python3
import os
import sys
from datetime import datetime, timezone
from botocore.exceptions import ClientError

sys.path.insert(0, "app")
from storage import RunPodStorage

TARGET_BYTES = 5 * 1024 ** 3
OBJ_BYTES = 100 * 1024 ** 2

def log(msg):
    print(msg, flush=True)

storage = RunPodStorage(bucket="v6a4zjiytb")
client, bucket = storage.client, storage.bucket

prefix = f"_diag_s3_capacity_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}/"
log(f"PREFIX: {prefix}")

uploaded_keys = []
accepted_objects = 0
accepted_bytes = 0
failure_summary = "FIRST_FAILURE: NONE - full target crossed successfully"

while accepted_bytes < TARGET_BYTES:
    index = accepted_objects + 1
    key = f"{prefix}part_{index:03d}.bin"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=os.urandom(OBJ_BYTES))
    except ClientError as e:
        http_status = e.response['ResponseMetadata']['HTTPStatusCode']
        code = e.response['Error']['Code']
        message = e.response['Error']['Message']
        request_id = e.response['ResponseMetadata'].get('RequestId')
        log(f"PUT_FAIL index={index} key={key} HTTPStatusCode={http_status} Code={code} Message={message} RequestId={request_id}")
        failure_summary = f"FIRST_FAILURE: index={index} key={key} HTTPStatusCode={http_status} Code={code} Message={message} RequestId={request_id}"
        break
    except Exception as e:
        log(f"PUT_FAIL index={index} key={key} {type(e).__name__}: {e}")
        failure_summary = f"FIRST_FAILURE: index={index} key={key} {type(e).__name__}: {e}"
        break

    uploaded_keys.append(key)
    accepted_objects = index
    accepted_bytes += OBJ_BYTES
    log(f"PUT_SUCCESS index={accepted_objects} key={key} PUT=SUCCESS cumulative_bytes={accepted_bytes} cumulative_gib={accepted_bytes / (1024**3):.6f}")

    if accepted_objects % 4 == 0:
        try:
            client.head_object(Bucket=bucket, Key=key)
            log(f"HEAD_CHECK index={accepted_objects} key={key} HEAD=SUCCESS")
        except ClientError as e:
            log(f"HEAD_CHECK index={accepted_objects} key={key} HEAD=FAIL Code={e.response['Error']['Code']} Message={e.response['Error']['Message']}")
        except Exception as e:
            log(f"HEAD_CHECK index={accepted_objects} key={key} HEAD=FAIL {type(e).__name__}: {e}")

log("---SUMMARY---")
log(f"OBJECTS_ACCEPTED: {accepted_objects}")
log(f"BYTES_ACCEPTED: {accepted_bytes}")
log(f"GIB_ACCEPTED: {accepted_bytes / (1024**3):.6f}")
log(failure_summary)

log("---CLEANUP---")
delete_success = 0
for key in uploaded_keys:
    try:
        client.delete_object(Bucket=bucket, Key=key)
        delete_success += 1
    except Exception as e:
        log(f"DELETE_FAIL key={key} {type(e).__name__}: {e}")
log(f"DELETE_SUCCESS_COUNT: {delete_success}")

remaining_keys = []
list_ok = True
try:
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            remaining_keys.append(obj['Key'])
except Exception as e:
    list_ok = False
    log(f"LIST_FAIL {type(e).__name__}: {e}")

if list_ok:
    log(f"REMAINING_KEY_COUNT: {len(remaining_keys)}")
    if remaining_keys:
        for key in remaining_keys:
            log(f"REMAINING_KEY: {key}")
else:
    log("REMAINING_KEY_COUNT: UNKNOWN")
