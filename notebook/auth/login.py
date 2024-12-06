"""Tornado handlers for logging into the notebook."""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import re

try:
    from urllib.parse import urlparse, urlunparse  # Py 3
except ImportError:
    from urlparse import urlparse, urlunparse  # Py 2
import uuid

from tornado.escape import url_escape

from ..auth.security import passwd_check

from ..base.handlers import IPythonHandler


class LoginHandler(IPythonHandler):
    """The basic tornado login handler

    authenticates with a hashed password from the configuration.
    """
    def _render(self, message=None):
        self.write(self.render_template('login.html',
                next=url_escape(self.get_argument('next', default=self.base_url)),
                message=message,
        ))

    def _redirect_safe(self, url, default=None):
        """Redirect if url is on our PATH

        Full-domain redirects are allowed if they pass our CORS origin checks.

        Otherwise use default (self.base_url if unspecified).
        """
        if default is None:
            default = self.base_url
        # protect chrome users from mishandling unescaped backslashes.
        # \ is not valid in urls, but some browsers treat it as /
        # instead of %5C, causing `\\` to behave as `//`
        url = url.replace("\\", "%5C")
        parsed = urlparse(url)
        path_only = urlunparse(parsed._replace(netloc='', scheme=''))
        if url != path_only or not (parsed.path + '/').startswith(self.base_url):
            # require that next_url be absolute path within our path
            allow = False
            # OR pass our cross-origin check
            if url != path_only:
                # if full URL, run our cross-origin check:
                origin = '%s://%s' % (parsed.scheme, parsed.netloc)
                origin = origin.lower()
                if origin == '%s://%s' % (self.request.protocol, self.request.host):
                    allow = True
                elif self.allow_origin:
                    allow = self.allow_origin == origin
                elif self.allow_origin_pat:
                    allow = bool(self.allow_origin_pat.match(origin))
            if not allow:
                # not allowed, use default
                self.log.warning("Not allowing login redirect to %r" % url)
                url = default
        self.redirect(url)

    def get(self):
        if self.current_user:
            next_url = self.get_argument('next', default=self.base_url)
            self._redirect_safe(next_url)
        else:
            self._render()

    @property
    def hashed_password(self):
        return self.password_from_settings(self.settings)

    def passwd_check(self, a, b):
        return passwd_check(a, b)
    
    def post(self):
        typed_password = self.get_argument('password', default=u'')
        if self.get_login_available(self.settings):
            if self.passwd_check(self.hashed_password, typed_password):
                self.set_login_cookie(self, uuid.uuid4().hex)
            elif self.token and self.token == typed_password:
                self.set_login_cookie(self, uuid.uuid4().hex)
            else:
                self.set_status(401)
                self._render(message={'error': 'Invalid password'})
                return

        next_url = self.get_argument('next', default=self.base_url)
        self._redirect_safe(next_url)

    @classmethod
    def set_login_cookie(cls, handler, user_id=None):
        """Call this on handlers to set the login cookie for success"""
        cookie_options = handler.settings.get('cookie_options', {})
        cookie_options.setdefault('httponly', True)
        # tornado <4.2 has a bug that considers secure==True as soon as
        # 'secure' kwarg is passed to set_secure_cookie
        if handler.settings.get('secure_cookie', handler.request.protocol == 'https'):
            cookie_options.setdefault('secure', True)
        handler.set_secure_cookie(handler.cookie_name, user_id, **cookie_options)
        return user_id

    auth_header_pat = re.compile('token\s+(.+)', re.IGNORECASE)

    @classmethod
    def get_token(cls, handler):
        """Get the user token from a request

        Default:

        - in URL parameters: ?token=<token>
        - in header: Authorization: token <token>
        """

        user_token = handler.get_argument('token', '')
        if not user_token:
            # get it from Authorization header
            m = cls.auth_header_pat.match(handler.request.headers.get('Authorization', ''))
            if m:
                user_token = m.group(1)
        return user_token

    @classmethod
    def should_check_origin(cls, handler):
        """Should the Handler check for CORS origin validation?

        Origin check should be skipped for token-authenticated requests.

        Returns:
        - True, if Handler must check for valid CORS origin.
        - False, if Handler should skip origin check since requests are token-authenticated.
        """
        return not cls.is_token_authenticated(handler)

    @classmethod
    def is_token_authenticated(cls, handler):
        """Returns True if handler has been token authenticated. Otherwise, False.

        Login with a token is used to signal certain things, such as:

        - permit access to REST API
        - xsrf protection
        - skip origin-checks for scripts
        """
        if getattr(handler, '_user_id', None) is None:
            # ensure get_user has been called, so we know if we're token-authenticated
            handler.get_current_user()
        return getattr(handler, '_token_authenticated', False)

    @classmethod
    def get_user(cls, handler):
        """Called by handlers.get_current_user for identifying the current user.

        See tornado.web.RequestHandler.get_current_user for details.
        """
        # Can't call this get_current_user because it will collide when
        # called on LoginHandler itself.
        if getattr(handler, '_user_id', None):
            return handler._user_id
        user_id = cls.get_user_token(handler)
        if user_id is None:
            user_id = handler.get_secure_cookie(handler.cookie_name)
        else:
            cls.set_login_cookie(handler, user_id)
            # Record that the current request has been authenticated with a token.
            # Used in is_token_authenticated above.
            handler._token_authenticated = True
        if user_id is None:
            # prevent extra Invalid cookie sig warnings:
            handler.clear_login_cookie()
            if not handler.login_available:
                # Completely insecure! No authentication at all.
                # No need to warn here, though; validate_security will have already done that.
                user_id = 'anonymous'

        # cache value for future retrievals on the same request
        handler._user_id = user_id
        return user_id

    @classmethod
    def get_user_token(cls, handler):
        """Identify the user based on a token in the URL or Authorization header
        
        Returns:
        - uuid if authenticated
        - None if not
        """
        token = handler.token
        if not token:
            return
        # check login token from URL argument or Authorization header
        user_token = cls.get_token(handler)
        one_time_token = handler.one_time_token
        authenticated = False
        if user_token == token:
            # token-authenticated, set the login cookie
            handler.log.debug("Accepting token-authenticated connection from %s", handler.request.remote_ip)
            authenticated = True
        elif one_time_token and user_token == one_time_token:
            # one-time-token-authenticated, only allow this token once
            handler.settings.pop('one_time_token', None)
            handler.log.info("Accepting one-time-token-authenticated connection from %s", handler.request.remote_ip)
            authenticated = True

        if authenticated:
            return uuid.uuid4().hex
        else:
            return None


    @classmethod
    def validate_security(cls, app, ssl_options=None):
        """Check the notebook application's security.

        Show messages, or abort if necessary, based on the security configuration.
        """
        if not app.ip:
            warning = "WARNING: The notebook server is listening on all IP addresses"
            if ssl_options is None:
                app.log.warning(warning + " and not using encryption. This "
                    "is not recommended.")
            if not app.password and not app.token:
                app.log.warning(warning + " and not using authentication. "
                    "This is highly insecure and not recommended.")
        else:
            if not app.password and not app.token:
                app.log.warning(
                    "All authentication is disabled."
                    "  Anyone who can connect to this server will be able to run code.")

    @classmethod
    def password_from_settings(cls, settings):
        """Return the hashed password from the tornado settings.

        If there is no configured password, an empty string will be returned.
        """
        return settings.get('password', u'')

    @classmethod
    def get_login_available(cls, settings):
        """Whether this LoginHandler is needed - and therefore whether the login page should be displayed."""
        return bool(cls.password_from_settings(settings) or settings.get('token'))
