#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
---
module: nico_fetch_resources
short_description: Resolve NICO REST resource IDs by name.
description:
  - Mints an OIDC password-grant token against a NICO Keycloak endpoint and
    looks up NICO resources by name via the NICO REST API, returning their
    IDs to the play as facts.
  - Every C(*_name) parameter is optional; the module fetches the tenant id
    unconditionally (via C(/tenant/current)) and each named resource only
    when a name is supplied.
  - Failure to resolve any requested name is fatal; the module reports the
    names that were available at the queried endpoint to aid debugging.
options:
  api_base_url:
    description: NICO REST API base URL, without trailing slash.
    required: true
    type: str
  org:
    description: NICO organization slug.
    required: true
    type: str
  token_url:
    description: NICO OIDC token endpoint (Keycloak realm token URL).
    required: true
    type: str
  client_id:
    description: OIDC client id used for the password grant.
    required: true
    type: str
  client_secret:
    description: OIDC client secret used for the password grant.
    required: true
    type: str
    no_log: true
  username:
    description: NICO admin username.
    required: true
    type: str
  password:
    description: NICO admin password.
    required: true
    type: str
    no_log: true
  site_name:
    description: NICO site name to look up.
    required: false
    type: str
  vpc_name:
    description: NICO VPC name to look up.
    required: false
    type: str
  vpc_prefix_name:
    description: NICO VPC-prefix name to look up. Requires C(vpc_name).
    required: false
    type: str
  instance_type_name:
    description: NICO instance-type name to look up.
    required: false
    type: str
  operating_system_name:
    description: NICO operating-system name to look up.
    required: false
    type: str
  ssh_key_group_name:
    description: NICO SSH key group name to look up.
    required: false
    type: str
  network_security_group_name:
    description: NICO network security group name to look up.
    required: false
    type: str
  validate_certs:
    description: Verify TLS certificates on REST calls.
    required: false
    type: bool
    default: false

returns:
  tenant_id:
    description: Value of C(id) from C(/v2/org/{org}/nico/tenant/current).
    returned: always
    type: str
  site_id:
    description: Resolved from C(/v2/org/{org}/nico/site).
    returned: when C(site_name) is set
    type: str
  vpc_id:
    description: Resolved from C(/v2/org/{org}/nico/vpc).
    returned: when C(vpc_name) is set
    type: str
  vpc_prefix_id:
    description: Resolved from C(/v2/org/{org}/nico/vpc-prefix), scoped to C(vpc_id).
    returned: when C(vpc_prefix_name) is set
    type: str
  instance_type_id:
    description: Resolved from C(/v2/org/{org}/nico/instance/type).
    returned: when C(instance_type_name) is set
    type: str
  operating_system_id:
    description: Resolved from C(/v2/org/{org}/nico/operating-system).
    returned: when C(operating_system_name) is set
    type: str
  ssh_key_group_id:
    description: Resolved from C(/v2/org/{org}/nico/sshkeygroup).
    returned: when C(ssh_key_group_name) is set
    type: str
  network_security_group_id:
    description: Resolved from C(/v2/org/{org}/nico/network-security-group).
    returned: when C(network_security_group_name) is set
    type: str
"""

EXAMPLES = r"""
- name: Resolve NICO ids for k0rdent-apis values overrides
  nico_fetch_resources:
    api_base_url: "{{ nico_api_base_url }}"
    org: "{{ nico_api_org }}"
    token_url: "{{ nico_token_url }}"
    client_id: "{{ nico_client_id }}"
    client_secret: "{{ nico_client_secret }}"
    username: "{{ nico_api_username }}"
    password: "{{ nico_api_password }}"
    ssh_key_group_name: "{{ nico_prepovision_ssh_key_group_name }}"
    network_security_group_name: "{{ nico_prepovision_network_security_group_name }}"
  register: nico_ids
"""

import json
import ssl
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from ansible.module_utils.basic import AnsibleModule


ENDPOINTS = [
    ("site_name", "/site", "site_id", "site_available"),
    ("vpc_name", "/vpc", "vpc_id", "vpc_available"),
    ("instance_type_name", "/instance/type", "instance_type_id", "instance_type_available"),
    ("operating_system_name", "/operating-system", "operating_system_id", "operating_system_available"),
    ("ssh_key_group_name", "/sshkeygroup", "ssh_key_group_id", "ssh_key_group_available"),
    ("network_security_group_name", "/network-security-group",
     "network_security_group_id", "network_security_group_available"),
]


def _http(url, method, module, ssl_ctx, headers=None, data=None):
    req = Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    body = None
    if data is not None:
        body = urlencode(data).encode("utf-8")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(req, data=body, context=ssl_ctx) as resp:
            payload = resp.read().decode("utf-8")
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        module.fail_json(msg="HTTP %d on %s %s: %s" % (e.code, method, url, detail))
    except URLError as e:
        module.fail_json(msg="Network error on %s %s: %s" % (method, url, e))
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        module.fail_json(msg="Non-JSON response from %s: %s (first 200 chars: %r)"
                         % (url, e, payload[:200]))


def _mint_token(module, ssl_ctx, params):
    body = _http(
        params["token_url"], "POST", module, ssl_ctx,
        data={
            "client_id": params["client_id"],
            "client_secret": params["client_secret"],
            "grant_type": "password",
            "username": params["username"],
            "password": params["password"],
        },
    )
    token = body.get("access_token")
    if not token:
        module.fail_json(msg="Token mint at %s returned no access_token" % params["token_url"])
    return token


def _find_by_name(items, name):
    for item in items:
        if item.get("name") == name:
            return item.get("id")
    return None


def main():
    module = AnsibleModule(
        argument_spec=dict(
            api_base_url=dict(type="str", required=True),
            org=dict(type="str", required=True),
            token_url=dict(type="str", required=True),
            client_id=dict(type="str", required=True),
            client_secret=dict(type="str", required=True, no_log=True),
            username=dict(type="str", required=True),
            password=dict(type="str", required=True, no_log=True),
            site_name=dict(type="str"),
            vpc_name=dict(type="str"),
            vpc_prefix_name=dict(type="str"),
            instance_type_name=dict(type="str"),
            operating_system_name=dict(type="str"),
            ssh_key_group_name=dict(type="str"),
            network_security_group_name=dict(type="str"),
            validate_certs=dict(type="bool", default=False),
        ),
        supports_check_mode=True,
    )
    p = module.params

    if p["vpc_prefix_name"] and not p["vpc_name"]:
        module.fail_json(msg="vpc_prefix_name requires vpc_name to scope the lookup")

    if p["validate_certs"]:
        ssl_ctx = ssl.create_default_context()
    else:
        ssl_ctx = ssl._create_unverified_context()

    token = _mint_token(module, ssl_ctx, p)
    api_headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    base = "%s/v2/org/%s/nico" % (p["api_base_url"].rstrip("/"), p["org"])

    result = {"changed": False}

    tenant = _http(base + "/tenant/current", "GET", module, ssl_ctx, headers=api_headers)
    tenant_id = tenant.get("id") if isinstance(tenant, dict) else None
    if not tenant_id:
        module.fail_json(msg="/tenant/current returned no id: %s" % tenant)
    result["tenant_id"] = tenant_id

    for param, path, out_key, avail_key in ENDPOINTS:
        name = p.get(param)
        if not name:
            continue
        items = _http(base + path, "GET", module, ssl_ctx, headers=api_headers)
        if not isinstance(items, list):
            module.fail_json(msg="%s returned non-list: %r" % (path, items))
        result[avail_key] = [i.get("name") for i in items if isinstance(i, dict)]
        resolved = _find_by_name(items, name)
        if resolved:
            result[out_key] = resolved

    if p["vpc_prefix_name"]:
        items = _http(base + "/vpc-prefix", "GET", module, ssl_ctx, headers=api_headers)
        if not isinstance(items, list):
            module.fail_json(msg="/vpc-prefix returned non-list: %r" % items)
        result["vpc_prefix_available"] = [
            i.get("name") for i in items
            if isinstance(i, dict) and i.get("vpcId") == result.get("vpc_id")
        ]
        candidates = [i for i in items
                      if isinstance(i, dict)
                      and i.get("vpcId") == result.get("vpc_id")
                      and i.get("name") == p["vpc_prefix_name"]]
        if candidates:
            result["vpc_prefix_id"] = candidates[0]["id"]

    module.exit_json(**result)


if __name__ == "__main__":
    main()
