# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
ClientToken that user can specify, and post requests
which idempotency is guaranteed by the previous ClientToken.

"""

import functools
import re

from oslo.config import cfg

from neutron.common import exceptions as exception
from neutron.openstack.common import log as logging
from neutron.openstack.common import memorycache

idempotent = [
    cfg.IntOpt('memcache_expiration',
               default=1 * 24 * 60 * 60,
               help='expire time of memory cache of x-client-token'),
]
CONF = cfg.CONF
CONF.register_opts(idempotent)

LOG = logging.getLogger(__name__)
CLIENT_TOKEN_PATTERN = re.compile('^[a-zA-Z0-9\.,-]{1,255}$')


def is_client_token(client_token):
    return re.match(CLIENT_TOKEN_PATTERN, client_token) is not None


def get_request(*args, **kwargs):
    headers = None
    url = None
    body = None
    tenant_id = None
    targets = ('req', 'request')
    for target in targets:
        if target in kwargs:
            headers = kwargs[target].headers
            url = kwargs[target].url
            body = kwargs[target].body
            if hasattr(kwargs[target], 'context'):
                tenant_id = kwargs[target].context.project_id
            else:
                for c in ('nova.context', 'cinder.context'):
                    if c in kwargs[target].environ:
                        tenant_id = kwargs[target].environ[c].project_id

            return kwargs[target], headers, url, body, tenant_id

    if isinstance(args[1], dict):
        headers = args[1]['headers']
        url = args[1]['headers']['Host'] + args[1]['path']
        body = kwargs
        tenant_id = 'dummy'
    else:
        headers = args[1].headers
        url = args[1].url
        body = kwargs['image_meta']
        tenant_id = args[1].context.tenant

    return args[1], headers, url, body, tenant_id


def helper(substance=None, resolver=None):
    def decorator(func):
        if substance:
            func.substance = substance
        if resolver:
            func.resolver = resolver
        return func
    return decorator


def idempotent(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        client_token = None
        value_list = []

        req, headers, url, body, tenant_id = get_request(*args, **kwargs)

        ### find X-CLIENT-TOKEN from req
        for key in headers.iterkeys():
            origin_key = key
            key = str(key.lower())
            if key == "x-client-token":
                client_token = headers.get(origin_key)
                if not is_client_token(client_token):
                    msg = "Invalid client_token: '%s'" % client_token
                    raise exception.InvalidClientToken(details=msg)
                break

        mc = memorycache.get_client()

        ### get action from specified substance, or detect id
        if client_token:
            stored = mc.get(client_token)

            if hasattr(f, 'substance'):
                target_action = (f.substance,)
            else:
                target_action = ('show', 'get', 'meta',
                                 'get_user', 'get_project')

            action = None
            for ta in target_action:
                if hasattr(args[0], ta):
                    action = ta
                    break
            if not action:
                msg = "There is no method to satisfy the request."
                raise exception.InvalidClientToken(details=msg)

            ### check client_token is used or not
            if stored and stored[1] == url:
                if stored[2] == body and stored[3] == tenant_id:
                    return getattr(args[0], action)(req, stored[0])
                elif stored[3] != tenant_id:
                    pass
                else:
                    msg = "Client_token '%s' is already used." % client_token
                    raise exception.InvalidClientToken(details=msg)

            ### execute POST method
            api_result = f(*args, **kwargs)
            data = api_result if isinstance(api_result, dict) else api_result.obj

            ### get id from specified resolver, or detect id
            resource = data.keys()[0]
            identifier = None
            if hasattr(f, 'resolver'):
                target_identifier = (f.resolver,)
            else:
                target_identifier = ('id', 'uuid', 'name')

            for ti in target_identifier:
                if data[resource].has_key(ti):
                    identifier = ti
                    break
            if not identifier:
                msg = "There is no identifier to satisfy the request."
                raise exception.InvalidClientToken(details=msg)

            ### make register data which is composed with
            if isinstance(api_result, dict):
                value_list.append(api_result[resource][identifier])
            else:
                value_list.append(api_result.obj[resource][identifier])
            value_list.append(url)
            value_list.append(body)
            value_list.append(tenant_id)

            mc.set(client_token, value_list, time=CONF.memcache_expiration)

            return api_result
        else:
            return f(*args, **kwargs)

    return inner
