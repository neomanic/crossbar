#####################################################################################
#
#  Copyright (c) Crossbar.io Technologies GmbH
#
#  Unless a separate license agreement exists between you and Crossbar.io GmbH (e.g.
#  you have purchased a commercial license), the license terms below apply.
#
#  Should you enter into a separate license agreement after having received a copy of
#  this software, then the terms of such license agreement replace the terms below at
#  the time at which such license agreement becomes effective.
#
#  In case a separate license agreement ends, and such agreement ends without being
#  replaced by another separate license agreement, the license terms below apply
#  from the time at which said agreement ends.
#
#  LICENSE TERMS
#
#  This program is free software: you can redistribute it and/or modify it under the
#  terms of the GNU Affero General Public License, version 3, as published by the
#  Free Software Foundation. This program is distributed in the hope that it will be
#  useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
#  See the GNU Affero General Public License Version 3 for more details.
#
#  You should have received a copy of the GNU Affero General Public license along
#  with this program. If not, see <http://www.gnu.org/licenses/agpl-3.0.en.html>.
#
#####################################################################################

from __future__ import absolute_import, division, print_function

import six
import txaio

from txaio import make_logger

from autobahn.wamp import message
from autobahn.wamp.exception import ProtocolError

from crossbar.router import RouterOptions
from crossbar.router.realmstore import HAS_LMDB, LmdbRealmStore, MemoryRealmStore
from crossbar.router.broker import Broker
from crossbar.router.dealer import Dealer
from crossbar.router.role import RouterRole, \
    RouterTrustedRole, RouterRoleStaticAuth, \
    RouterRoleDynamicAuth

__all__ = (
    'RouterFactory',
)


def _is_client_session(session):
    return hasattr(session, '_session_details')


class Router(object):
    """
    Crossbar.io core router class.
    """
    log = make_logger()

    RESERVED_ROLES = [u'trusted']
    """
    Roles with these URIs are built-in and cannot be added/dropped.
    """

    broker = Broker
    """
    The broker class this router will use.
    """

    dealer = Dealer
    """
    The dealer class this router will use.
    """

    def __init__(self, factory, realm, options=None, store=None, mqtt_payload_format=None):
        """

        :param factory: The router factory this router was created by.
        :type factory: Object that implements :class:`autobahn.wamp.interfaces.IRouterFactory`..
        :param realm: The realm this router is working for.
        :type realm: str
        :param options: Router options.
        :type options: Instance of :class:`crossbar.router.RouterOptions`.
        :param mqtt_payload_format: The format that MQTT messages on this realm are in.
        :type mqtt_payload_format: str
        """
        self._factory = factory
        self._options = options or RouterOptions()
        self._store = store
        self._realm = realm
        self.realm = realm.config[u'name']

        self._trace_traffic = False
        self._trace_traffic_roles_include = None
        self._trace_traffic_roles_exclude = [u'trusted']

        # map: session_id -> session
        self._session_id_to_session = {}

        self._broker = self.broker(self, self._options)
        self._dealer = self.dealer(self, self._options)
        self._attached = 0

        self._mqtt_payload_format = mqtt_payload_format

        self._roles = {
            u'trusted': RouterTrustedRole(self, u'trusted')
        }

    def attach(self, session):
        """
        Implements :func:`autobahn.wamp.interfaces.IRouter.attach`
        """
        if session._session_id not in self._session_id_to_session:
            if _is_client_session(session):
                self._session_id_to_session[session._session_id] = session
            else:
                self.log.debug("attaching non-client session {session}",
                               session=session)
        else:
            raise Exception("session with ID {} already attached".format(session._session_id))

        self._broker.attach(session)
        self._dealer.attach(session)

        self._attached += 1

        return {u'broker': self._broker._role_features, u'dealer': self._dealer._role_features}

    def detach(self, session):
        """
        Implements :func:`autobahn.wamp.interfaces.IRouter.detach`
        """
        self._broker.detach(session)
        self._dealer.detach(session)

        if session._session_id in self._session_id_to_session:
            del self._session_id_to_session[session._session_id]
        else:
            if _is_client_session(session):
                raise Exception("session with ID {} not attached".format(session._session_id))

        self._attached -= 1
        if not self._attached:
            self._factory.onLastDetach(self)

    def _check_trace(self, session, msg):
        if not self._trace_traffic:
            return False
        if self._trace_traffic_roles_include and session._authrole not in self._trace_traffic_roles_include:
            return False
        if self._trace_traffic_roles_exclude and session._authrole in self._trace_traffic_roles_exclude:
            return False
        return True

    def send(self, session, msg):
        if self._check_trace(session, msg):
            self.log.info("<<TX<< {msg}", msg=msg)
        session._transport.send(msg)

    def process(self, session, msg):
        """
        Implements :func:`autobahn.wamp.interfaces.IRouter.process`
        """
        if self._check_trace(session, msg):
            self.log.info(">>RX>> {msg}", msg=msg)

        # Broker
        #
        if isinstance(msg, message.Publish):
            self._broker.processPublish(session, msg)

        elif isinstance(msg, message.Subscribe):
            self._broker.processSubscribe(session, msg)

        elif isinstance(msg, message.Unsubscribe):
            self._broker.processUnsubscribe(session, msg)

        # Dealer
        #
        elif isinstance(msg, message.Register):
            self._dealer.processRegister(session, msg)

        elif isinstance(msg, message.Unregister):
            self._dealer.processUnregister(session, msg)

        elif isinstance(msg, message.Call):
            self._dealer.processCall(session, msg)

        elif isinstance(msg, message.Cancel):
            self._dealer.processCancel(session, msg)

        elif isinstance(msg, message.Yield):
            self._dealer.processYield(session, msg)

        elif isinstance(msg, message.Error) and msg.request_type == message.Invocation.MESSAGE_TYPE:
            self._dealer.processInvocationError(session, msg)

        else:
            raise ProtocolError("Unexpected message {0}".format(msg.__class__))

    def has_role(self, uri):
        """
        Check if a role with given URI exists on this router.

        :returns: bool - `True` if a role under the given URI exists on this router.
        """
        return uri in self._roles

    def add_role(self, role):
        """
        Adds a role to this router.

        :param role: The role to add.
        :type role: An instance of :class:`crossbar.router.session.CrossbarRouterRole`.

        :returns: bool -- `True` if a role under the given URI actually existed before and was overwritten.
        """
        self.log.debug("CrossbarRouter.add_role({role})", role=role)

        if role.uri in self.RESERVED_ROLES:
            raise Exception("cannot add reserved role '{}'".format(role.uri))

        overwritten = role.uri in self._roles

        self._roles[role.uri] = role

        return overwritten

    def drop_role(self, role):
        """
        Drops a role from this router.

        :param role: The role to drop.
        :type role: An instance of :class:`crossbar.router.session.CrossbarRouterRole`.

        :returns: bool -- `True` if a role under the given URI actually existed and was removed.
        """
        self.log.debug("CrossbarRouter.drop_role({role})", role=role)

        if role.uri in self.RESERVED_ROLES:
            raise Exception("cannot drop reserved role '{}'".format(role.uri))

        if role.uri in self._roles:
            del self._roles[role.uri]
            return True
        else:
            return False

    def authorize(self, session, uri, action):
        """
        Authorizes a session for an action on an URI.

        Implements :func:`autobahn.wamp.interfaces.IRouter.authorize`
        """
        assert(type(uri) == six.text_type)
        assert(action in [u'call', u'register', u'publish', u'subscribe'])

        # the role under which the session that wishes to perform the given action on
        # the given URI was authenticated under
        role = session._authrole

        if role in self._roles:
            # the authorizer procedure of the role which we will call ..
            authorize = self._roles[role].authorize
            d = txaio.as_future(authorize, session, uri, action)
        else:
            # normally, the role should exist on the router (and hence we should not arrive
            # here), but the role might have been dynamically removed - and anyway, safety first!
            d = txaio.create_future_success(False)

        def got_authorization(authorization):
            # backward compatibility
            if isinstance(authorization, bool):
                authorization = {
                    u'allow': authorization,
                    u'cache': False
                }
                if action in [u'call', u'publish']:
                    authorization[u'disclose'] = False

            auto_disclose_trusted = False
            if auto_disclose_trusted and role == u'trusted' and action in [u'call', u'publish']:
                authorization[u'disclose'] = True

            self.log.debug("Authorized action '{action}' for URI '{uri}' by session {session_id} with authid '{authid}' and authrole '{authrole}' -> authorization: {authorization}",
                           session_id=session._session_id,
                           uri=uri,
                           action=action,
                           authid=session._authid,
                           authrole=session._authrole,
                           authorization=authorization)

            return authorization

        d.addCallback(got_authorization)
        return d

    def validate(self, payload_type, uri, args, kwargs):
        """
        Implements :func:`autobahn.wamp.interfaces.IRouter.validate`
        """
        self.log.debug("Validate '{payload_type}' for '{uri}'",
                       payload_type=payload_type, uri=uri, cb_level="trace")


class RouterFactory(object):
    """
    Crossbar.io core router factory.
    """
    log = make_logger()
    router = Router
    """
    The router class this factory will create router instances from.
    """

    def __init__(self, options=None):
        """

        :param options: Default router options.
        :type options: Instance of :class:`crossbar.router.RouterOptions`.
        """
        self._routers = {}
        self._options = options or RouterOptions(uri_check=RouterOptions.URI_CHECK_LOOSE)
        self._auto_create_realms = False

    def get(self, realm):
        """
        Implements :func:`autobahn.wamp.interfaces.IRouterFactory.get`
        """
        if self._auto_create_realms:
            if realm not in self._routers:
                self._routers[realm] = self.router(self, realm, self._options)
                self.log.debug("Router created for realm '{realm}'",
                               realm=realm)
            return self._routers[realm]
        else:
            return self._routers[realm]

    def __getitem__(self, realm):
        return self._routers[realm]

    def __contains__(self, realm):
        return realm in self._routers

    def onLastDetach(self, router):
        assert(router.realm in self._routers)
        del self._routers[router.realm]
        self.log.debug("Router destroyed for realm '{realm}'",
                       realm=router.realm)

    def start_realm(self, realm):
        """
        Starts a realm on this router.

        :param realm: The realm to start.
        :type realm: instance of :class:`crossbar.worker.router.RouterRealm`.

        :returns: The router instance for the started realm.
        :rtype: instance of :class:`crossbar.router.session.CrossbarRouter`
        """
        self.log.debug("CrossbarRouterFactory.start_realm(realm = {realm})",
                       realm=realm)

        # get name of realm (an URI in general)
        #
        uri = realm.config['name']
        assert(uri not in self._routers)

        # if configuration of realm contains a "store" item, set up a
        # realm store as appropriate ..
        store = None
        if 'store' in realm.config:
            store_config = realm.config['store']

            if store_config['type'] == 'lmdb':
                # if LMDB is available, and a realm store / database is configured,
                # create an LMDB environment
                if not HAS_LMDB:
                    raise Exception("LDMB not available")
                store = LmdbRealmStore(store_config)

            elif store_config['type'] == 'memory':
                store = MemoryRealmStore(store_config)
            else:
                raise Exception('logic error')

        mqtt_payload_format = "opaque"
        if 'mqtt_payload_format' in realm.config:
            mqtt_payload_format = realm.config['mqtt_payload_format']

            if mqtt_payload_format not in ["opaque", "json", "cbor"]:
                raise Exception("Not a valid mqtt_payload_format.")

        # now create a router for the realm
        #
        router = Router(self, realm, self._options, store=store,
                        mqtt_payload_format=mqtt_payload_format)
        self._routers[uri] = router
        self.log.debug("Router created for realm '{uri}'", uri=uri)

        return router

    def stop_realm(self, realm):
        self.log.debug("CrossbarRouterFactory.stop_realm(realm = {realm})",
                       realm=realm)

    def add_role(self, realm, config):
        self.log.debug("CrossbarRouterFactory.add_role(realm = {realm}, config = {config})",
                       realm=realm, config=config)

        assert(type(realm) == six.text_type)
        assert(realm in self._routers)

        router = self._routers[realm]
        uri = config[u'name']

        if u'permissions' in config:
            role = RouterRoleStaticAuth(router, uri, config[u'permissions'])
        elif u'authorizer' in config:
            role = RouterRoleDynamicAuth(router, uri, config[u'authorizer'])
        else:
            allow_by_default = config.get(u'allow-by-default', False)
            role = RouterRole(router, uri, allow_by_default=allow_by_default)

        router.add_role(role)

    def drop_role(self, realm, role):
        self.log.debug("CrossbarRouterFactory.drop_role(realm = {realm}, role = {role})",
                       realm=realm, role=role)

    def auto_start_realm(self, realm):
        raise Exception("realm auto-activation (realm '{}') not yet implemented".format(realm))

    def auto_add_role(self, realm, role):
        raise Exception("role auto-activation (role '{}') not yet implemented".format(role))
