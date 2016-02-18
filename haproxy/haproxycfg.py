import copy
import logging
from collections import OrderedDict

import helper.backend_helper as BackendHelper
import helper.config_helper as ConfigHelper
import helper.frontend_helper as FrontendHelper
import helper.init_helper as InitHelper
import helper.ssl_helper as SslHelper
import helper.tcp_helper as TcpHelper
import helper.update_helper as UpdateHelper
from haproxy.config import *
from parser import Specs
from utils import fetch_remote_obj, prettify, save_to_file, get_service_attribute, get_bind_string

logger = logging.getLogger("haproxy")


def run_haproxy(msg=None):
    haproxy = Haproxy(msg)
    haproxy.update()


class Haproxy(object):
    cls_linked_services = []
    cls_cfg = None
    cls_process = None
    cls_certs = []

    cls_service_name_match = re.compile(r"(.+)_\d+$")

    LINKED_CONTAINER_CACHE = {}

    def __init__(self, msg=""):
        logger.info("==========BEGIN==========")
        if msg:
            logger.info(msg)

        self.ssl_bind_string = None
        self.ssl_updated = False
        self.routes_added = []
        self.require_default_route = False

        self._initialize()

    def _initialize(self):
        if HAPROXY_CONTAINER_URI and HAPROXY_SERVICE_URI and API_AUTH:
            haproxy_container = fetch_remote_obj(HAPROXY_CONTAINER_URI)

            haproxy_links = InitHelper.get_links_from_haproxy(haproxy_container.linked_to_container)
            new_added_container_uris = InitHelper.get_new_added_link_uri(Haproxy.LINKED_CONTAINER_CACHE, haproxy_links)
            new_added_containers = InitHelper.get_container_object_from_uri(new_added_container_uris)
            InitHelper.update_container_cache(Haproxy.LINKED_CONTAINER_CACHE, new_added_container_uris,
                                              new_added_containers)
            linked_containers = InitHelper.get_linked_containers(Haproxy.LINKED_CONTAINER_CACHE,
                                                                 haproxy_container.linked_to_container)
            InitHelper.update_haproxy_links(haproxy_links, linked_containers)

            logger.info("Service links: %s", ", ".join(InitHelper.get_service_links_str(haproxy_links)))
            logger.info("Container links: %s", ", ".join(InitHelper.get_container_links_str(haproxy_links)))

            Haproxy.cls_linked_services = InitHelper.get_linked_services(haproxy_links)
            self.specs = Specs(haproxy_links)
        else:
            logger.info("Loading HAProxy definition from environment variables")
            Haproxy.cls_linked_services = None
            Haproxy.specs = Specs()

    def update(self):
        self._config_ssl()

        cfg_dict = OrderedDict()
        cfg_dict.update(self._config_global_section())
        cfg_dict.update(self._config_defaults_section())
        cfg_dict.update(self._config_stats_section())
        cfg_dict.update(self._config_userlist_section(HTTP_BASIC_AUTH))
        cfg_dict.update(self._config_tcp_sections())
        cfg_dict.update(self._config_frontend_sections())
        cfg_dict.update(self._config_backend_sections())

        cfg = prettify(cfg_dict)
        self._update_haproxy(cfg)

    def _update_haproxy(self, cfg):
        if HAPROXY_SERVICE_URI and HAPROXY_CONTAINER_URI and API_AUTH:
            if Haproxy.cls_cfg != cfg:
                logger.info("HAProxy configuration:\n%s" % cfg)
                Haproxy.cls_cfg = cfg
                if save_to_file(HAPROXY_CONFIG_FILE, cfg):
                    Haproxy.cls_process = UpdateHelper.run_reload(Haproxy.cls_process)
            elif self.ssl_updated:
                logger.info("SSL certificates have been changed")
                Haproxy.cls_process = UpdateHelper.run_reload(Haproxy.cls_process)
            else:
                logger.info("HAProxy configuration remains unchanged")
            logger.info("===========END===========")
        else:
            logger.info("HAProxy configuration:\n%s" % cfg)
            save_to_file(HAPROXY_CONFIG_FILE, cfg)
            UpdateHelper.run_once()

    def _config_ssl(self):
        ssl_bind_string = ""
        ssl_bind_string += self._config_ssl_certs()
        ssl_bind_string += self._config_ssl_cacerts()
        if ssl_bind_string:
            self.ssl_bind_string = ssl_bind_string

    def _config_ssl_certs(self):
        ssl_bind_string = ""
        certs = []
        if DEFAULT_SSL_CERT:
            certs.append(DEFAULT_SSL_CERT)
        certs.extend(SslHelper.get_extra_ssl_certs(EXTRA_SSL_CERT))
        certs.extend(self.specs.get_default_ssl_cert())
        certs.extend(self.specs.get_ssl_cert())
        if certs:
            if set(certs) != set(Haproxy.cls_certs):
                Haproxy.cls_certs = copy.copy(certs)
                self.ssl_updated = True
                SslHelper.save_certs(CERT_DIR, certs)
                logger.info("SSL certificates are updated")
            ssl_bind_string = "ssl crt /certs/"
        return ssl_bind_string

    def _config_ssl_cacerts(self):
        ssl_bind_string = ""
        cacerts = []
        if DEFAULT_CA_CERT:
            cacerts.append(DEFAULT_CA_CERT)
        if cacerts:
            if set(cacerts) != set(Haproxy.cls_certs):
                Haproxy.cls_certs = copy.copy(cacerts)
                self.ssl_updated = True
                SslHelper.save_certs(CACERT_DIR, cacerts)
                logger.info("SSL CA certificates are updated")
            ssl_bind_string = " ca-file /cacerts/cert0.pem verify required"
        return ssl_bind_string

    @staticmethod
    def _config_global_section():
        cfg = OrderedDict()

        statements = ["log %s local0" % RSYSLOG_DESTINATION,
                      "log %s local1 notice" % RSYSLOG_DESTINATION,
                      "log-send-hostname",
                      "maxconn %s" % MAXCONN,
                      "pidfile /var/run/haproxy.pid",
                      "user haproxy",
                      "group haproxy",
                      "daemon",
                      "stats socket /var/run/haproxy.stats level admin"]

        statements.extend(ConfigHelper.config_ssl_bind_options(SSL_BIND_OPTIONS))
        statements.extend(ConfigHelper.config_ssl_bind_ciphers(SSL_BIND_CIPHERS))
        statements.extend(ConfigHelper.config_extra_settings(EXTRA_GLOBAL_SETTINGS))
        cfg["global"] = statements
        return cfg

    @staticmethod
    def _config_stats_section():
        cfg = OrderedDict()
        bind = " ".join([STATS_PORT, EXTRA_BIND_SETTINGS.get(STATS_PORT, "")])
        cfg["listen stats"] = ["bind :%s" % bind.strip(),
                               "mode http",
                               "stats enable",
                               "timeout connect 10s",
                               "timeout client 1m",
                               "timeout server 1m",
                               "stats hide-version",
                               "stats realm Haproxy\ Statistics",
                               "stats uri /",
                               "stats auth %s" % STATS_AUTH]
        return cfg

    @staticmethod
    def _config_defaults_section():
        cfg = OrderedDict()
        statements = ["balance %s" % BALANCE,
                      "log global",
                      "mode %s" % MODE]

        statements.extend(ConfigHelper.config_option(OPTION))
        statements.extend(ConfigHelper.config_timeout(TIMEOUT))
        statements.extend(ConfigHelper.config_extra_settings(EXTRA_DEFAULT_SETTINGS))

        cfg["defaults"] = statements
        return cfg

    @staticmethod
    def _config_userlist_section(basic_auth):
        cfg = OrderedDict()
        if basic_auth:
            auth_list = re.split(r'(?<!\\),', basic_auth)
            userlist = []
            for auth in auth_list:
                if auth.strip():
                    terms = auth.strip().split(":", 1)
                    if len(terms) == 2:
                        username = terms[0].replace("\,", ",")
                        password = terms[1].replace("\,", ",")
                        userlist.append("user %s insecure-password %s" % (username, password))

            if userlist:
                cfg["userlist haproxy_userlist"] = userlist
        return cfg

    def _config_tcp_sections(self):
        details = self.specs.get_details()
        services_aliases = self.specs.get_service_aliases()

        cfg = OrderedDict()
        if not get_service_attribute(details, "tcp_ports"):
            return cfg

        tcp_ports = TcpHelper.get_tcp_port_list(details, services_aliases)

        for tcp_port in set(tcp_ports):
            tcp_section, port_num = self.get_tcp_section(details, services_aliases, tcp_port)
            cfg["listen port_%s" % port_num] = tcp_section
        return cfg

    def get_tcp_section(self, details, services_aliases, tcp_port):
        tcp_section = []
        enable_ssl, port_num = TcpHelper.parse_port_string(tcp_port, self.ssl_bind_string)
        bind_string = get_bind_string(enable_ssl, port_num, self.ssl_bind_string, EXTRA_BIND_SETTINGS)
        tcp_routes, self.routes_added = TcpHelper.get_tcp_routes(details, self.specs.get_routes(), tcp_port, port_num)
        services = TcpHelper.get_service_aliases_given_tcp_port(details, services_aliases, tcp_port)
        balance = TcpHelper.get_tcp_balance(details)
        options = TcpHelper.get_tcp_options(details, services)
        extra_settings = TcpHelper.get_tcp_extra_settings(details, services)
        tcp_section.append("bind :%s" % bind_string.strip())
        tcp_section.append("mode tcp")
        tcp_section.extend(balance)
        tcp_section.extend(options)
        tcp_section.extend(extra_settings)
        tcp_section.extend(tcp_routes)
        return tcp_section, port_num

    def _config_frontend_sections(self):
        vhosts = self.specs.get_vhosts()
        ssl_bind_string = self.ssl_bind_string
        monitor_uri_configured = False
        if vhosts:
            cfg, monitor_uri_configured = FrontendHelper.config_frontend_with_virtual_host(vhosts, ssl_bind_string)
        else:
            self.require_default_route = FrontendHelper.check_require_default_route(self.specs.get_routes(),
                                                                                    self.routes_added)
            if self.require_default_route:
                cfg, monitor_uri_configured = FrontendHelper.config_default_frontend(ssl_bind_string)
            else:
                cfg = OrderedDict()

        cfg.update(FrontendHelper.config_monitor_frontend(monitor_uri_configured))
        return cfg

    def _config_backend_sections(self):
        details = self.specs.get_details()
        routes = self.specs.get_routes()
        vhosts = self.specs.get_vhosts()
        cfg = OrderedDict()

        if not self.specs.get_vhosts():
            services_aliases = [None]
        else:
            services_aliases = self.specs.get_service_aliases()

        for service_alias in services_aliases:
            backend = BackendHelper.get_backend_section(details, routes, vhosts, service_alias, self.routes_added)

            if not service_alias:
                if self.require_default_route:
                    cfg["backend default_service"] = backend
            else:
                if get_service_attribute(details, "virtual_host", service_alias):
                    cfg["backend SERVICE_%s" % service_alias] = backend
                else:
                    cfg["backend default_service"] = backend
        return cfg
