#!/usr/bin/env python

# Copyright (c) 2014, Palo Alto Networks
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# Author: Brian Torres-Gil <btorres-gil@paloaltonetworks.com>


"""Base object classes for inheritence by other classes"""

import re
import xml.etree.ElementTree as ET
import logging
import inspect
import time

import pandevice

import pan.xapi
from pan.config import PanConfig
import errors as err
import updater

# set logging to nullhandler to prevent exceptions if logging not enabled
logger = logging.getLogger(__name__)

Root = pandevice.enum("DEVICE", "VSYS", "MGTCONFIG")
ENTRY = "/entry[@name='%s']"
MEMBER = "/member[text()='%s']"


# PanObject type
class PanObject(object):
    XPATH = "/config"
    SUFFIX = None
    ROOT = Root.DEVICE
    NAME = "name"
    CHILDTYPES = ()
    CHILDMETHODS = ()

    def __init__(self, name=None):
        self.name = name
        self.parent = None
        self.children = []

    def __str__(self):
        return str(self.name)

    @staticmethod
    def vars():
        return ()

    def add(self, child):
        child.parent = self
        self.children.append(child)
        return self

    def extend(self, children):
        for child in children:
            child.parent = self
        self.children.extend(children)

    def pop(self, index):
        child = self.children.pop(index)
        child.parent = None
        return child

    def remove_by_name(self, name, cls=None):
        index = PanObject.find(self.children, name, cls)
        if index is None:
            return None
        return self.pop(index)  # Just remove the first child that matches the name

    def xpath(self):
        """Return an xpath for this object

        Xpath in the form: parent's xpath + this object's xpath + entry or member if applicable.
        """
        if self.parent is None:
            parent_xpath = ""
        elif issubclass(type(self.parent), PanDevice):
            parent_xpath = self.parent.xpath_root(self.ROOT)
        else:
            parent_xpath = self.parent.xpath()
        suffix = "" if self.SUFFIX is None else self.SUFFIX % self.name
        result = str(parent_xpath + self.XPATH + suffix)
        return result

    def element(self):
        root = self.root_element()
        variables = self.vars()
        for var in variables:
            value = vars(self)[var.variable]
            if value is None:
                continue
            path = var.path.split("/")
            next = root
            for section in path:
                if section.find("|") != -1:
                    # This is an element variable, so break out at this point
                    break
                # Search for variable replacements in path
                matches = re.findall(r'{{(.*?)}}', section)
                entryvar = None
                for match in matches:
                    regex = r'{{' + re.escape(match) + r'}}'
                    for nextvar in variables:
                        if nextvar.variable == match:
                            matchedvar = nextvar
                    if matchedvar.vartype == "entry":
                        section = re.sub(regex,
                                         matchedvar.path + "/" + "entry[@name='%s']" % vars(self)[matchedvar.variable],
                                         section)
                        entryvar = matchedvar
                    else:
                        section = re.sub(regex, vars(self)[matchedvar.variable], section)
                found = next.find(section)
                if found is not None:
                    # Existing element
                    next = found
                else:
                    # Create elements
                    if entryvar is not None:
                        # for vartype="entry"
                        next = ET.SubElement(next, entryvar.path)
                        next = ET.SubElement(next, "entry", {"name": vars(self)[entryvar.variable]})
                    else:
                        # for vartype="entry"
                        next = ET.SubElement(next, section)
            # Create an element containing the value in the instance variable
            if var.vartype == "member":
                for member in value:
                    ET.SubElement(next, 'member').text = str(member)
            elif var.vartype == "entry":
                try:
                    # Value is an array
                    for entry in value:
                        ET.SubElement(next, 'entry', {'name': str(entry)})
                except TypeError:
                    # Value is not an array
                    ET.SubElement(next, 'entry', {'name': str(value)})
            elif var.vartype == "exist":
                if value:
                    ET.SubElement(next, var.variable)
            elif var.vartype == "bool":
                next.text = "yes" if value else "no"
            elif var.path.find("|") != -1:
                # This is an element variable
                ET.SubElement(next, str(value))
            else:
                next.text = str(value)
        root.extend(self.subelements())
        return root

    def element_str(self):
        return ET.tostring(self.element())

    def root_element(self):
        if self.SUFFIX == ENTRY:
            return ET.Element("entry", {'name': self.name})
        elif self.SUFFIX == MEMBER:
            root = ET.Element("member")
            root.text = self.name
            return root
        elif self.SUFFIX is None:
            tag = self.XPATH.rsplit('/', 1)[-1] # Get right of last / in xpath
            return ET.Element(tag)

    def subelements(self):

        def _next_xpath_level(child, element, xpath_sections):
            """Recursive nested method to handle long xpaths"""
            if not xpath_sections:
                element.append(child.element())
                return
            next_section = xpath_sections[0]
            next_element = element.find(next_section)
            if next_element is None:
                next_element = ET.SubElement(element, xpath_sections[0])
            _next_xpath_level(child,
                              next_element,
                              xpath_sections[1:])
            return

        elements = ET.Element('root')
        for child in self.children:
            # Get the extra layers in the next node's xpath
            xpath_sections = type(child).XPATH.split('/')[1:]
            # If no suffix, remove the last xpath section
            # because it will be part of the element
            if self.SUFFIX is None:
                xpath_sections = xpath_sections[:-1]
            _next_xpath_level(child, elements, xpath_sections)
        # Return a list of subelements
        return [element for element in elements]

    def apply(self):
        self.pandevice().xapi.edit(self.xpath(), self.element_str())

    def create(self):
        # Remove the last part from the xpath
        xpath = re.sub(r"/(?=([^/']*'[^']*')[^/']*$).*", "", self.xpath())
        self.pandevice().xapi.set(xpath, self.element_str())
        for child in self.children:
            if "create" in self.CHILDMETHODS:
                child.create()

    def delete(self):
        self.pandevice().xapi.delete(self.xpath())
        if self.parent is not None:
            self.parent.remove_by_name(self.name, type(self))
        for child in self.children:
            if "delete" in self.CHILDMETHODS:
                child.delete()

    def refresh(self, candidate=False, xml=None):
        # Get the root of the xml to parse
        if xml is None:
            pandevice = self.pandevice()
            if candidate:
                api_action = pandevice.xapi.show
            else:
                api_action = pandevice.xapi.get
            api_action(self.xpath())
            root = pandevice.xapi.element_root
            # Determine the first element to look for in the XML
            if self.SUFFIX is None:
                lasttag = self.XPATH.rsplit("/", 1)[-1]
            else:
                lasttag = re.match(r'^/(\w*?)\[', self.SUFFIX).group(1)
            # lasttag = self.xpath().rsplit("/", 1)[-1]
            obj = root.find("result/" + lasttag)
            if obj is None:
                raise err.PanDeviceError("Object no longer exists!")
        else:
            # Use the xml that was passed in
            obj = xml
        # Refresh each variable
        variables = type(self)._parse_xml(obj)
        for var, value in variables.iteritems():
            vars(self)[var] = value

    def pandevice(self):
        if issubclass(self.__class__, PanDevice):
            return self
        else:
            if self.parent is None:
                raise err.PanDeviceNotSet("No PanDevice set for object tree")
            else:
                return self.parent.pandevice()

    def find(self, name, class_type=None):
        if class_type is None:
            indexes = [i for i, child in enumerate(self.children) if
                       child.name == name and isinstance(child, class_type)]
        else:
            indexes = [i for i, child in enumerate(self.children) if child.name == name]
        for index in indexes:
            return self.children[index]  # Just return the first object that matches the name
        return None

    def find_all(self, class_type):
        return [child for child in self.children if isinstance(child, class_type)]

    @classmethod
    def find(cls, list_of_panobjects, name, class_type=None):
        if class_type is None:
            class_type = cls
        indexes = [i for i, child in enumerate(list_of_panobjects) if
                   child.name == name and isinstance(child, class_type)]
        for index in indexes:
            return index  # Just return the first index that matches the name
        return None

    @classmethod
    def refresh_all_from_device(cls, pandevice, candidate=False, update=True):
        """Factory method to instantiate class from firewall config

        This method is a factory for the class. It takes an firewall or Panorama
        and gets the xml config from the device. It generates instances of this
        class for each item this class represents in the xml config. For example,
        if the class is AddressObject and there are 5 address objects on the
        firewall, then this method will generate 5 instances of the class AddressObject.

        Args:
            pandevice (PanDevice): A firewall or Panorama object
            candidate (bool): False for running config, True for candidate config
            update (bool): Update the objects of this type in pandevice with
                the refreshed values

        Returns:
            list: created instances of class
        """
        if candidate:
            api_action = pandevice.xapi.show
        else:
            api_action = pandevice.xapi.get
        api_action(pandevice.xpath_root(cls.ROOT) + cls.XPATH)
        root = pandevice.xapi.element_root
        lasttag = cls.XPATH.rsplit("/", 1)[-1]
        obj = root.find("result/" + lasttag)
        # Refresh each object
        return cls.refresh_all_from_xml(obj)

    @classmethod
    def refresh_all_from_xml(cls, xml, refresh_children=True):
        """Factory method to instantiate class from firewall config

        This method is a factory for the class. It takes an xml config
        from a firewall and generates instances of this class for each item
        this class represents in the xml config. For example, if the class is
        AddressObject and there are 5 address objects on the firewall, then
        this method will generate 5 instances of the class AddressObject.

        Args:
            xml (Element): A section of XML configuration from a firewall or Panorama

        Returns:
            list: created instances of class
        """
        instances = []
        if cls.SUFFIX is None:
            objects = [xml]
        else:
            lasttag = re.match(r'^/(\w*?)\[', cls.SUFFIX).group(1)
            objects = xml.findall(lasttag)
        # Refresh each object
        for obj in objects:
            variables, noinit_variables = cls._parse_xml(obj)
            name = obj.get('name')
            if name is not None:
                variables[cls.NAME] = name
            instance = cls(**variables)
            # Set values of no init variables
            for var, value in noinit_variables.iteritems():
                vars(instance)[var] = value
            instances.append(instance)
            # Refresh the children of these instances
            if refresh_children:
                for childtype in cls.CHILDTYPES:
                    childroot = obj.find(childtype.XPATH[1:])
                    if childroot is not None:
                        l = childtype.refresh_all_from_xml(childroot)
                        instance.extend(l)
        return instances

    @classmethod
    def _parse_xml(cls, xml):
        variables = {}
        noinit_variables = {}
        # Parse each variable
        vars = cls.vars()
        for var in vars:
            if var.init:
                vardict = variables
            else:
                vardict = noinit_variables
            if var.vartype == "member":
                members = xml.findall(var.path + "/member")
                vardict[var.variable] = [m.text for m in members]
            elif var.vartype == "entry":
                entries = xml.findall(var.path + "/entry")
                vardict[var.variable] = [e.get("name") for e in entries]
            elif var.vartype == "exist":
                match = xml.find(var.path)
                vardict[var.variable] = True if match is not None else False
            else:
                if var.path.find("|") != -1:
                    # This is an element variable
                    sections = var.path.split("/")
                    options = sections.pop()
                    path = "/".join(sections)
                    if path != "":
                        path += "/"
                    options = options.split("|")
                    found = False
                    for opt in options:
                        match = xml.find(path + opt)
                        if match is not None:
                            vardict[var.variable] = cls._convert_var(opt, var.vartype)
                            found = True
                            break
                    if not found:
                        vardict[var.variable] = None
                else:
                    # This is a text variable
                    # Search for variable replacements in path
                    path = var.path
                    matches = re.findall(r'{{(.*?)}}', path)
                    for match in matches:
                        regex = r'{{' + re.escape(match) + r'}}'
                        path = re.sub(regex, vardict[match], path)
                    try:
                        # Save the variable if it exists in the xml
                        vardict[var.variable] = cls._convert_var(xml.find(path).text, var.vartype)
                    except AttributeError:
                        # Couldn't find the path in the xml
                        vardict[var.variable] = None
                if var.default is not None and vardict[var.variable] is None:
                    vardict[var.variable] = var.default
        return variables, noinit_variables

    @classmethod
    def _convert_var(cls, value, vartype):
        if vartype is None:
            return value
        elif vartype == "int":
            return int(value)
        elif vartype == "bool":
            return True if value == "yes" else False


class VarPath(object):
    """Configuration variable within the object

    Attributes:
        path (string): The relative xpath to the variable
        variable (string): The name of the instance variable in the class
        vartype (string): The type of variable (None or 'member')
    """
    def __init__(self, path, variable=None, vartype=None, default=None, init=True):
        self.path = path
        self._variable = variable
        self.vartype = vartype
        self.default = default
        self.init = init

    @property
    def variable(self):
        if self._variable is None:
            return self.path.rsplit("/", 1)[-1].replace('-','_')
        else:
            return self._variable

    @variable.setter
    def variable(self, value):
        self._variable = value


class VsysImportMixin(object):
    """Modify PanObject methods to set vsys import configuration

    This only applies to some object types, hence it is a Mixin,
    and not part of PanObject
    """
    XPATH_IMPORT = None
    CHILDMETHODS = ("create", "delete")

    def __init__(self, *args, **kwargs):
        super(VsysImportMixin, self).__init__(*args, **kwargs)

    def create(self, *args, **kwargs):
        super(VsysImportMixin, self).create(*args, **kwargs)
        pandevice = super(VsysImportMixin, self).pandevice()
        if pandevice.vsys != "shared":
            xpath_import = self.XPATH_IMPORT if self.XPATH_IMPORT is not None else self.XPATH
            xpath_import = pandevice.xpath_vsys() + "/import" + xpath_import
            pandevice.xapi.set(xpath_import, "<member>%s</member>" % self.name)

    def delete(self, *args, **kwargs):
        pandevice = super(VsysImportMixin, self).pandevice()
        xpath_import = self.XPATH_IMPORT if self.XPATH_IMPORT is not None else self.XPATH
        xpath_import = pandevice.xpath_vsys() + "/import" + xpath_import
        if pandevice.vsys != "shared":
            pandevice.xapi.delete(xpath_import + "/member[text()='%s']" % self.name)
            super(VsysImportMixin, self).delete(*args, **kwargs)


class PanDevice(PanObject):
    """A Palo Alto Networks device

    The device can be of any type (currently supported devices are firewall,
    or panorama). The class handles common device functions that apply
    to all device types.

    Attributes:
        hostname: Hostname or IP of device for API connections
        port: Port of device for API connections
        vsys: This device class represents a specific VSYS
        devicegroup: This device class represents a specific Device-Group
            in Panorama
        xpath: The XPath for the root of this device, taking into account any
            VSYS, Device-Group, or Panorama state
        timeout: The timeout for API connections
        api_key: The API Key for connecting to the device's API
    """

    def __init__(self,
                 hostname,
                 api_username=None,
                 api_password=None,
                 api_key=None,
                 port=443,
                 is_virtual=None,
                 timeout=1200,
                 interval=.5,
                 classify_exceptions=False):
        """Initialize PanDevice"""
        super(PanDevice, self).__init__()
        # create a class logger
        self._logger = logging.getLogger(__name__ + "." + self.__class__.__name__)

        self.hostname = hostname
        self.port = port
        self._api_username = api_username
        self._api_password = api_password
        self._api_key = api_key
        self.is_virtual = is_virtual
        self.timeout = timeout
        self.interval = interval
        self.interfaces = {}
        self._xapi_private = None
        self._classify_exceptions = classify_exceptions
        self.config_locked = False
        self.commit_locked = False
        self.lock_before_change = False
        self.config_changed = False
        self.connected_to_panorama = None
        self.dg_in_sync = None

        # Create a PAN-OS updater subsystem
        self.software = updater.SoftwareUpdater(self)
        # Create a content updater subsystem
        self.content = updater.ContentUpdater(self)

        # State variables
        self.version = None
        self.content_version = None

    @classmethod
    def create_from_device(cls,
                           hostname,
                           api_username=None,
                           api_password=None,
                           api_key=None,
                           port=443,
                           classify_exceptions=False):
        """Create a Firewall or Panorama object from a live device

        This method connects to the device and detects its type and current
        state in order to create a PanDevice subclass.

        :returns PanDevice subclass instance (Firewall or Panorama instance)
        """
        # Create generic PanDevice to connect and get information
        import firewall
        import panorama
        device = PanDevice(hostname,
                           api_username,
                           api_password,
                           api_key,
                           port,
                           classify_exceptions=classify_exceptions)
        version, model, serial = device.system_info()
        if model == "Panorama":
            subclass = panorama.Panorama
        else:
            subclass = firewall.Firewall
        instance = subclass(hostname,
                            api_username,
                            api_password,
                            device.api_key,
                            serial,
                            port,
                            classify_exceptions=classify_exceptions)
        instance.version = version
        return instance

    class XapiWrapper(pan.xapi.PanXapi):
        """This is a confusing class used for catching exceptions and
        faults.
        """
        # TODO: comment the hell out of it!

        def __init__(self, *args, **kwargs):
            self.pan_device = kwargs.pop('pan_device', None)
            pan.xapi.PanXapi.__init__(self, *args, **kwargs)

            for name, method in inspect.getmembers(
                    pan.xapi.PanXapi,
                    inspect.ismethod):
                # Ignore hidden methods
                if name[0] == "_":
                    continue

                # Wrapper method.  This is used to create
                # methods in this class that match the methods in the
                # super class, and call the super class methods inside
                # a try/except block, which allows us to check and
                # analyze the exceptions and convert them to more
                # useful exceptions than generic PanXapiErrors.
                wrapper_method = self.make_method(method)

                # Create method matching each public method of the base class
                setattr(PanDevice.XapiWrapper, name, wrapper_method)

        def make_method(self, super_method):
            def method(*args, **kwargs):
                try:
                    return super_method(*args, **kwargs)
                except pan.xapi.PanXapiError as e:
                    if e.msg == "Invalid credentials.":
                        raise err.PanInvalidCredentials(
                            e.msg,
                            pan_device=self.pan_device,
                            )
                    elif e.msg.startswith("URLError:"):
                        if e.msg.endswith("timed out"):
                            raise err.PanConnectionTimeout(
                                e.msg,
                                pan_device=self.pan_device,
                                )
                        else:
                            raise err.PanURLError(e.msg,
                                                  pan_device=self.pan_device)

                    elif e.msg.startswith("timeout waiting for job"):
                        raise err.PanJobTimeout(e.msg,
                                                pan_device=self.pan_device)

                    elif e.msg.startswith("Another commit/validate is in"
                                          " progress. Please try again later"):
                        raise err.PanCommitInProgress(e.msg,
                                                      pan_device=self.pan_device)

                    elif e.msg.startswith("A commit is in progress."):
                        raise err.PanCommitInProgress(e.msg,
                                                      pan_device=self.pan_device)

                    elif e.msg.startswith("You cannot commit while an install is in progress. Please try again later."):
                        raise err.PanInstallInProgress(e.msg,
                                                       pan_device=self.pan_device)

                    elif e.msg.startswith("Session timed out"):
                        raise err.PanSessionTimedOut(e.msg,
                                                     pan_device=self.pan_device)

                    else:
                        raise err.PanDeviceXapiError(e.msg,
                                                     pan_device=self.pan_device)

            return method

    # Generate from a live device (firewall or Panorama)
    #@classmethod
    #def create_from_live_device(self, hostname, username=None, password=None, apikey=None, port=443):
    #    pass

    # Properties

    @property
    def api_key(self):
        if self._api_key is None:
            self._api_key = self._retrieve_api_key()
        return self._api_key

    @property
    def xapi(self):
        if self._xapi_private is None:
            self._xapi_private = self.generate_xapi()
        return self._xapi_private

    def update_connection_method(self):
        self._xapi_private = self.generate_xapi()
        return self._xapi_private

    def generate_xapi(self):
        kwargs = {'api_key': self.api_key,
                  'hostname': self.hostname,
                  'port': self.port,
                  'timeout': self.timeout,
                  }
        if self._classify_exceptions:
            xapi_constructor = PanDevice.XapiWrapper
            kwargs['pan_device'] = self,
        else:
            xapi_constructor = pan.xapi.PanXapi
        return xapi_constructor(**kwargs)

    def set_config_changed(self):
        if self.lock_before_change:
            if not self.config_locked:
                self.add_config_lock(exception=True)
                """
                if self.pending_changes():
                    self.revert_to_running_configuration()
                    raise err.PanPendingChanges("There are pending changes, "
                                            "cannot apply configuration "
                                            "because cannot get config-lock",
                                            pan_device=self)
                """
        self.config_changed = True

    def xpath_root(self, root_type):
        if root_type == Root.DEVICE:
            xpath = self.xpath_device()
        elif root_type == Root.VSYS:
            xpath = self.xpath_vsys()
        elif root_type == Root.MGTCONFIG:
            xpath = self.xpath_mgtconfig()
        elif root_type == Root.PANORAMA:
            xpath = self.xpath_panorama()
        else:
            xpath = self.XPATH
        return xpath

    def xpath_mgtconfig(self):
        return self.XPATH + "/mgt-config"

    def xpath_device(self):
        return self.XPATH + "/devices/entry[@name='localhost.localdomain']"

    def xpath_vsys(self):
        raise NotImplementedError

    def xpath_panorama(self):
        raise NotImplementedError

    def __get_xpath_scope(self):
        """Return the XPath root for the current device

        A private helper method to return an XPath that is appropriate given
        the current state of the instance variables. This XPath represents
        the root of the VSYS, Device-Group, or Shared object area.

        Returns:
            A string containing an XPath to be used as the root for
            other API calls
        """
        xpath_device = "/config/devices/entry[@name='localhost.localdomain']"
        xpath_vsys = xpath_device + "/vsys/entry[@name='%s']"
        xpath_devicegroup = xpath_device + "device-group/entry[@name='%s']"
        xpath_shared = "/config/shared"

        if self.devicegroup:
            return xpath_devicegroup % self.devicegroup
        elif self.is_panorama:
            return xpath_shared
        else:
            return xpath_device

    def _retrieve_api_key(self):
        """Return an API key for a username and password

        Given a username and password, return the API key of that user for
        this PAN Device. The username and password are not stored, and the
        API key is returned.  It is up to the caller to store it in an
        instance variable if desired.

        Returns:
            A string containing the API key

        Raises:
            PanDeviceError: If unable to retrieve the API key for reasons
                other than an API connectivity problem
            PanXapiError:  Raised by pan.xapi module for API errors
        """
        self._logger.debug("Getting API Key from %s for user %s" %
                           (self.hostname, self._api_username))
        if self._classify_exceptions:
            xapi = PanDevice.XapiWrapper(
                pan_device=self,
                api_username=self._api_username,
                api_password=self._api_password,
                hostname=self.hostname,
                port=self.port,
                timeout=self.timeout
            )
        else:
            xapi = pan.xapi.PanXapi(
                api_username=self._api_username,
                api_password=self._api_password,
                hostname=self.hostname,
                port=self.port,
                timeout=self.timeout
            )
        xapi.keygen()
        return xapi.api_key

    def system_info(self, all_info=False):
        """Get system information

        Returns:
            system information like version, platform, etc.
        """

        self.xapi.op(cmd="<show><system><info></info></system></show>")
        pconf = PanConfig(self.xapi.element_result)
        system_info = pconf.python()
        self._logger.debug("Systeminfo: %s" % system_info)
        if not system_info:
            error_msg = 'Cannot detect device type, unable to get system info'
            self._logger.error(error_msg)
            raise err.PanDeviceError(error_msg, pan_device=self)

        if not all_info:
            version = system_info['result']['system']['sw-version']
            model = system_info['result']['system']['model']
            serial = system_info['result']['system']['serial']
            return version, model, serial
        else:
            return system_info['result']

    def refresh_version(self):
        """Get version of PAN-OS

        returns:
            version of PAN-OS
        """
        system_info = self.system_info()
        self.version = system_info[0]
        return self.version

    def set_hostname(self, hostname):
        self._logger.debug("Set hostname: %s" % (hostname,))
        self.set_config_changed()
        xpath = pandevice.XPATH_DEVICECONFIG_SYSTEM
        self.xapi.set(xpath, "<hostname>%s</hostname>" % (hostname,))

    def set_dns_servers(self, primary, secondary=None):
        self._logger.debug("Set dns-servers: primary:%s secondary:%s" % (primary, secondary))
        xpath = pandevice.XPATH_DEVICECONFIG_SYSTEM + "/dns-setting/servers"
        element = ET.Element("servers")
        if primary:
            element_primary = ET.SubElement(element, "primary")
            element_primary.text = primary
        if secondary:
            element_secondary = ET.SubElement(element, "secondary")
            element_secondary.text = secondary
        self.xapi.edit(xpath, ET.tostring(element))

    def set_ntp_servers(self, primary, secondary=None):
        self._logger.debug("Set ntp-servers: primary:%s secondary:%s" % (primary, secondary))
        self.set_config_changed()
        xpath = pandevice.XPATH_DEVICECONFIG_SYSTEM
        xpath61 = pandevice.XPATH_DEVICECONFIG_SYSTEM + "/ntp-servers"
        # Path is different depending on PAN-OS 6.0 vs 6.1
        # Try PAN-OS 6.1 first
        element61 = ""

        # First if primary is None, remove all NTP config
        if primary is None:
            # PAN-OS 6.1 and higher
            self.xapi.delete(xpath61)
            # PAN-OS 6.0 and lower
            self.xapi.delete(xpath + "/ntp-server-1")
            self.xapi.delete(xpath + "/ntp-server-2")
            return

        if primary:
            element61 += "<ntp-servers>" \
                         "<primary-ntp-server>" \
                         "<ntp-server-address>%s</ntp-server-address>" \
                         "</primary-ntp-server>" % (primary,)
        if secondary:
            element61 += "<secondary-ntp-server>" \
                         "<ntp-server-address>%s</ntp-server-address>" \
                         "</secondary-ntp-server>" % (secondary,)
        element61 += "</ntp-servers>"

        try:
            # PAN-OS 6.1 and higher
            self.xapi.edit(xpath61, element61)
            self._logger.debug("Set ntp server for PAN-OS 6.1 or higher")
        except (pan.xapi.PanXapiError, err.PanDeviceXapiError) as e:
            try:
                message = e.msg
            except AttributeError:
                message = e.message
            if message.startswith("Could not get schema node for xpath"):
                # PAN-OS 6.0 and lower
                self._set_ntp_servers_60(primary, secondary=secondary)
                self._logger.debug("Set ntp server for PAN-OS 6.0 or lower")
            else:
                self._logger.debug("Could not set NTP server, unknown PAN-OS version")
                raise e

    def _set_ntp_servers_60(self, primary, secondary=None):
        """Set ntp servers on PAN-OS 6.0 and lower"""
        xpath60 = pandevice.XPATH_DEVICECONFIG_SYSTEM
        xpath60_pri = xpath60 + "/ntp-server-1"
        xpath60_sec = xpath60 + "/ntp-server-2"
        element60_pri = ""
        element60_sec = ""

        if primary:
            element60_pri += "<ntp-server-1>%s</ntp-server-1>" % (primary,)
        if secondary:
            element60_sec += "<ntp-server-2>%s</ntp-server-2>" % (secondary,)
        self.xapi.edit(xpath60_pri, element60_pri)
        self.xapi.edit(xpath60_sec, element60_sec)

    def show_interface(self, interface):
        self.set_config_changed()
        interface_name = self._interface_name(interface)

        self.xapi.op("<show><interface>%s</interface></show>" % (interface_name,))
        pconf = PanConfig(self.xapi.element_result)
        response = pconf.python()
        return response['result']

    def pending_changes(self):
        self.xapi.op(cmd="check pending-changes", cmd_xml=True)
        pconf = PanConfig(self.xapi.element_result)
        response = pconf.python()
        return response['result']

    def add_commit_lock(self, comment=None, exception=False):
        self._logger.debug("Add commit lock requested")
        cmd = ET.Element("request")
        subel = ET.SubElement(cmd, "commit-lock")
        subel = ET.SubElement(subel, "add")
        if comment is not None:
            subel = ET.SubElement(subel, "comment")
            subel.text = comment
        try:
            self.xapi.op(ET.tostring(cmd))
        except (pan.xapi.PanXapiError, err.PanDeviceXapiError) as e:
            if not re.match(r"Commit lock is already held", str(e)):
                raise
            else:
                if exception:
                    raise err.PanLockError(str(e), pan_device=self)
                else:
                    self._logger.debug(str(e))
                    return False
        self.commit_locked = True
        return True

    def remove_commit_lock(self, admin=None, exception=False):
        self._logger.debug("Remove commit lock requested")
        cmd = ET.Element("request")
        subel = ET.SubElement(cmd, "commit-lock")
        subel = ET.SubElement(subel, "remove")
        if admin is not None:
            subel = ET.SubElement(subel, "admin")
            subel.text = admin
        try:
            self.xapi.op(ET.tostring(cmd))
        except (pan.xapi.PanXapiError, err.PanDeviceXapiError) as e:
            if not re.match(r"Commit lock is not currently held", str(e)):
                raise
            else:
                if exception:
                    raise err.PanLockError(str(e), pan_device=self)
                else:
                    self._logger.debug(str(e))
                    return False
        self.commit_locked = False
        return True

    def add_config_lock(self, comment=None, exception=False):
        self._logger.debug("Add config lock requested")
        cmd = ET.Element("request")
        subel = ET.SubElement(cmd, "config-lock")
        subel = ET.SubElement(subel, "add")
        if comment is not None:
            subel = ET.SubElement(subel, "comment")
            subel.text = comment
        try:
            self.xapi.op(ET.tostring(cmd))
        except (pan.xapi.PanXapiError, err.PanDeviceXapiError) as e:
            if not re.match(r"Config for scope shared is currently locked",
                            str(e)):
                raise
            else:
                if exception:
                    raise err.PanLockError(str(e), pan_device=self)
                else:
                    self._logger.debug(str(e))
                    return False
        self.config_locked = True
        return True

    def remove_config_lock(self, exception=False):
        self._logger.debug("Remove config lock requested")
        cmd = ET.Element("request")
        subel = ET.SubElement(cmd, "config-lock")
        subel = ET.SubElement(subel, "remove")
        try:
            self.xapi.op(ET.tostring(cmd))
        except (pan.xapi.PanXapiError, err.PanDeviceXapiError) as e:
            if not re.match(r"Config is not currently locked for scope shared",
                            str(e)):
                raise
            else:
                if exception:
                    raise err.PanLockError(str(e), pan_device=self)
                else:
                    self._logger.debug(str(e))
                    return False
        self.config_locked = False
        return True

    def check_commit_locks(self):
        self.xapi.op("show commit-locks", cmd_xml=True)
        response = self.xapi.element_result.find(".//entry")
        return True if response is not None else False

    def revert_to_running_configuration(self):
        # self.set_config_changed()
        self._logger.debug("Revert to running configuration on device: %s" % (self.hostname,))
        self.xapi.op("<load><config><from>"
                     "running-config.xml"
                     "</from></config></load>")

    def restart(self):
        self._logger.debug("Requesting restart on device: %s" % (self.hostname,))
        try:
            self.xapi.op("request restart system", cmd_xml=True)
        except pan.xapi.PanXapiError as e:
            if not e.msg.startswith("Command succeeded with no output"):
                raise e


    def refresh_devices_from_panorama(self, devices=()):
        try:
            # Test if devices is iterable
            test_iterable = iter(devices)
        except TypeError:
            # This probably means a single device was passed in, not an iterable.
            # Convert to an iterable with a single item.
            devices = (devices,)
        stats_by_ip = {}
        stats_by_host = {}
        devicegroup_stats_by_serial = {}
        template_stats_by_serial = {}
        # Get the list of managed devices
        self.xapi.op("show devices all", cmd_xml=True)
        pconf = PanConfig(self.xapi.element_root)
        response = pconf.python()
        try:
            for device in response['response']['result']['devices']['entry']:
                stats_by_ip[device['ip-address']] = device
                stats_by_host[device['ip-address']] = device
                stats_by_host[device['hostname']] = device
            # Populate the device objects with some of the data
            for device in devices:
                try:
                    device.serial = stats_by_host[device.hostname]['serial']
                    device.connected_to_panorama = stats_by_host[device.hostname]['connected']
                except KeyError as e:
                    raise err.PanDeviceError("Can't determine serial for "
                                             "device", pan_device=device)
        # Ignore errors because it means there are no devices
        except KeyError:
            return {}

        # Get the list of device groups
        self.xapi.op("show devicegroups", cmd_xml=True)
        dg_element = self.xapi.element_result
        for dg in dg_element.findall("./devicegroups/entry"):
            for device in dg.findall("./devices/entry"):
                pconf = PanConfig(config=device)
                stats = pconf.python()
                # Save device stats
                stats = stats['entry']
                # Save device serial
                serial = stats['serial']
                # Save device ip-address
                ip = stats['ip-address']
                # Save device's device-group
                dg_name = dg.get('name')
                # Save the device-group to the device's stats
                stats['devicegroup'] = dg_name
                devicegroup_stats_by_serial[serial] = stats
                stats_by_ip[ip]['devicegroup'] = dg_name

        # Set the device-group for each device
        for device in devices:
            if device.serial is not None:
                stats = devicegroup_stats_by_serial.get(device.serial)
                if stats is not None:
                    device.devicegroup = stats['devicegroup']
                    sync_status = stats['shared-policy-status']
                    device.dg_in_sync = True if sync_status == "In Sync" else False

        return stats_by_ip

    def commit(self, sync=False, exception=False, cmd=None):
        self._logger.debug("Commit initiated on device: %s" % (self.hostname,))
        return self._commit(sync=sync, exception=exception, cmd=cmd)

    def _commit(self, cmd=None, exclude=None, commit_all=False,
                sync=False, sync_all=True, exception=False):
        """Internal use commit helper method.

        :param exclude:
            Can be:
                device-and-network
                policy-and-objects
        :param sync:
            Synchronous commit, ie. wait for job to finish
        :return:
            Result of commit as dict if synchronous.  JobID if asynchronous.
            In either case, if no commit is needed, return None.
            Most important fields in dict:
                success:  True or False
                result:  OK or FAIL
                messages: list of warnings or errors

        """

        if issubclass(cmd.__class__, pan.commit.PanCommit):
            cmd = cmd.cmd()
        elif issubclass(cmd.__class__, ET.Element):
            cmd = ET.tostring(cmd)
        elif issubclass(cmd.__class__, basestring):
            pass
        else:
            cmd = ET.Element("commit")
            if exclude is not None:
                excluded = ET.SubElement(cmd, "partial")
                excluded = ET.SubElement(excluded, exclude)
            cmd = ET.tostring(cmd)
        if commit_all:
            action = "all"
        else:
            action = None
        if sync:
            self._logger.debug("Waiting for commit job to finish...")
        self.xapi.commit(cmd=cmd,
                         action=action,
                         sync=sync,
                         sync_all=sync_all,
                         interval=self.interval,
                         timeout=self.timeout)
        self.config_changed = False
        self.config_locked = False
        self.commit_locked = False
        if sync:
            pconf = PanConfig(self.xapi.element_result)
            response = pconf.python()
            job = response['result']
            if job is None:
                if exception:
                    raise err.PanCommitNotNeeded("Commit not needed",
                                                 pan_device=self)
                else:
                    return
            job = job['job']
            # Create a boolean called success to make
            # testing for success easier
            devices_results = {}
            devices_success = True
            if commit_all and sync_all:
                devices = job['devices']
                if devices is not None:
                    devices = devices['entry']
                    for device in devices:
                        success = True if device['result'] == "OK" else False
                        if not success:
                            devices_success = False
                        devices_results[device['serial-no']] = {
                            'success': success,
                            'serial': device['serial-no'],
                            'name': device['devicename'],
                            'result': device['result'],
                            'starttime': device['tstart'],
                            'endtime': device['tfin'],
                            }
                        # Errors and warnings might not have a full structure.  If it is just a string, then
                        # a TypeError will be produced, so in that case, just grab the string.
                        try:
                            devices_results[device['serial-no']]['warnings'] = device['details']['msg']['warnings']
                        except TypeError as e:
                            devices_results[device['serial-no']]['warnings'] = ""
                        try:
                            devices_results[device['serial-no']]['messages'] = device['details']['msg']['errors'][
                                'line']
                        except TypeError as e:
                            devices_results[device['serial-no']]['messages'] = device['details']

            success = True if job['result'] == "OK" and devices_success else False

            if commit_all:
                messages = []
            else:
                messages = job['details']['line']
            if issubclass(messages.__class__, basestring):
                messages = [messages]

            # Create the results dict
            result = {
                'success': success,
                'result': job['result'],
                'jobid': job['id'],
                'user': job['user'],
                'warnings': job['warnings'],
                'starttime': job['tenq'],
                'endtime': job['tfin'],
                'messages': messages,
                'devices': devices_results
            }

            if exception and not success:
                self._logger.debug("Commit failed - device: %s, job: %s, messages: %s, warnings: %s" %
                                   (self.hostname,
                                    result['jobid'],
                                    result['messages'],
                                    result['warnings']))
                raise err.PanCommitFailed(pan_device=self, result=result)
            else:
                if success:
                    self._logger.debug("Commit succeeded - device: %s, job: %s, messages: %s, warnings: %s" %
                                       (self.hostname,
                                        result['jobid'],
                                        result['messages'],
                                        result['warnings']))
                else:
                    self._logger.debug("Commit failed - device: %s, job: %s, messages: %s, warnings: %s" %
                                       (self.hostname,
                                        result['jobid'],
                                        result['messages'],
                                        result['warnings']))
                return result
        else:
            jobid = self.xapi.element_root.find('./result/job')
            if jobid is None:
                if exception:
                    raise err.PanCommitNotNeeded("Commit not needed",
                                                 pan_device=self)
                else:
                    return
            self._logger.debug("Commit initiated (async), job id: %s" % (jobid,))
            return jobid


    def syncjob(self, response, interval=0.5):
        """Block until job completes and return result

        response: XML response tag from firewall when job is created

        :returns True if job completed successfully, False if not
        """
        if interval is not None:
            try:
                interval = float(interval)
                if interval < 0:
                    raise ValueError
            except ValueError:
                raise err.PanDeviceError('Invalid interval: %s' % interval)

        job = response.find('./result/job')
        if job is None:
            return False
        job = job.text

        self._logger.debug('Syncing job: %s', job)

        cmd = 'show jobs id "%s"' % job
        start_time = time.time()

        while True:
            try:
                self.xapi.op(cmd=cmd, cmd_xml=True)
            except pan.xapi.PanXapiError as msg:
                raise pan.xapi.PanXapiError('commit %s: %s' % (cmd, msg))

            path = './result/job/status'
            status = self.xapi.element_root.find(path)
            if status is None:
                raise pan.xapi.PanXapiError('No status element in ' +
                                            "'%s' response" % cmd)
            if status.text == 'FIN':
                pconf = PanConfig(self.xapi.element_result)
                response = pconf.python()
                job = response['result']
                if job is None:
                    return
                job = job['job']
                success = True if job['result'] == "OK" else False
                messages = job['details']['line']
                if issubclass(messages.__class__, basestring):
                    messages = [messages]
                # Create the results dict
                result = {
                    'success': success,
                    'result': job['result'],
                    'jobid': job['id'],
                    'user': job['user'],
                    'warnings': job['warnings'],
                    'starttime': job['tenq'],
                    'endtime': job['tfin'],
                    'messages': messages,
                }
                return result

            self._logger.debug('Job %s status %s', job, status.text)

            if (self.timeout is not None and self.timeout != 0 and
                        time.time() > start_time + self.timeout):
                raise pan.xapi.PanXapiError('Timeout waiting for ' +
                                            'job %s completion' % job)

            self._logger.debug('Sleep %.2f seconds', interval)
            time.sleep(interval)

    def syncreboot(self, interval=5.0, timeout=600):
        """Block until reboot completes and return version of device"""

        import httplib

        # Validate interval and convert it to float
        if interval is not None:
            try:
                interval = float(interval)
                if interval < 0:
                    raise ValueError
            except ValueError:
                raise err.PanDeviceError("Invalid interval: %s" % interval)

        self._logger.debug("Syncing reboot...")

        # Record start time to gauge timeout
        start_time = time.time()
        attempts = 0
        is_rebooting = False

        time.sleep(interval)
        while True:
            try:
                # Try to get the device version (ie. test to see if firewall is up)
                attempts += 1
                version = self.refresh_version()
            except (pan.xapi.PanXapiError, err.PanDeviceXapiError) as e:
                # Connection errors (URLError) are ok
                # Invalid cred errors are ok because FW auth system takes longer to start up
                # Other errors should be raised
                if not e.msg.startswith("URLError:") and not e.msg.startswith("Invalid credentials."):
                    # Error not related to connection issue.  Raise it.
                    raise e
                else:
                    # Connection issue.  The firewall is currently rebooting.
                    is_rebooting = True
                    self._logger.debug("Connection attempted: %s" % str(e))
                    self._logger.debug("Device is not available yet. Connection attempts: %s" % str(attempts))
            except httplib.BadStatusLine as e:
                # Connection issue.  The firewall is currently rebooting.
                is_rebooting = True
                self._logger.debug("Connection attempted: %s" % str(e))
                self._logger.debug("Device is not available yet. Connection attempts: %s" % str(attempts))
            else:
                # No exception... connection succeeded and device is up!
                # This could mean reboot hasn't started yet, so check that we had
                # a connection error prior to this success.
                if is_rebooting:
                    self._logger.debug("Device is up! Running version %s" % version)
                    return version
                else:
                    self._logger.debug("Device is up, but it probably hasn't started rebooting yet.")

            # Check to see if we hit timeout
            if (self.timeout is not None and self.timeout != 0 and
                        time.time() > start_time + self.timeout):
                raise err.PanDeviceError("Timeout waiting for device to reboot")

            # Sleep and try again
            self._logger.debug("Sleep %.2f seconds", interval)
            time.sleep(interval)