from __future__ import unicode_literals

import copy
import importlib

from django.db import models
from django.db.models.fields.related import RelatedField
from django.conf import settings
from django.contrib import admin
from .manager import HistoryDescriptor
from registration import FieldRegistry
from django.contrib.auth.models import User

try:
    basestring
except NameError:
    basestring = str  # Python 3 has no basestring

try:
    from django.utils.encoding import python_2_unicode_compatible
except ImportError:  # django 1.3 compatibility
    import sys

    # copy of django function without use of six
    def python_2_unicode_compatible(klass):
        """
        Decorator defining __unicode__ and __str__ as appropriate for Py2/3

        Usage: define __str__ method and apply this decorator to the class.
        """
        if sys.version_info[0] != 3:
            klass.__unicode__ = klass.__str__
            klass.__str__ = lambda self: self.__unicode__().encode('utf-8')
        return klass


registered_models = {}

# This is used to store the user id - else just None.
class CurrentUserField(models.ForeignKey):
    def __init__(self, **kwargs):
        kwargs['null'] = True
        kwargs.pop('to', None)
        super(CurrentUserField, self).__init__(User, **kwargs)

    def contribute_to_class(self, cls, name):
        super(CurrentUserField, self).contribute_to_class(cls, name)
        registry = FieldRegistry()
        registry.add_field(cls, self)


try:
    import south
except ImportError:
    pass
else:
    from south.modelsinspector import add_introspection_rules
    add_introspection_rules([], ["simple_history.models.CurrentUserField"])


class HistoricalRecords(object):
    def contribute_to_class(self, cls, name):
        self.manager_name = name
        self.module = cls.__module__
        models.signals.class_prepared.connect(self.finalize, sender=cls)

        def save_without_historical_record(self, *args, **kwargs):
            """
            Save model without saving a historical record

            Make sure you know what you're doing before you use this method.
            """
            self.skip_history_when_saving = True
            try:
                ret = self.save(*args, **kwargs)
            finally:
                del self.skip_history_when_saving
            return ret
        setattr(cls, 'save_without_historical_record',
                save_without_historical_record)

    def finalize(self, sender, **kwargs):
        history_model = self.create_history_model(sender)
        module = importlib.import_module(self.module)
        setattr(module, history_model.__name__, history_model)

        # The HistoricalRecords object will be discarded,
        # so the signal handlers can't use weak references.
        models.signals.post_save.connect(self.post_save, sender=sender,
                                         weak=False)
        models.signals.post_delete.connect(self.post_delete, sender=sender,
                                           weak=False)

        descriptor = HistoryDescriptor(history_model)
        setattr(sender, self.manager_name, descriptor)
        sender._meta.simple_history_manager_attribute = self.manager_name

    def create_history_model(self, model):
        """
        Creates a historical model to associate with the model provided.
        """
        attrs = {'__module__': self.module}

        app_module = '%s.models' % model._meta.app_label
        if model.__module__ != self.module:
            # registered under different app
            attrs['__module__'] = self.module
        elif app_module != self.module:
            # has meta options with app_label
            app = models.get_app(model._meta.app_label)
            attrs['__module__'] = app.__name__  # full dotted name

        fields = self.copy_fields(model)
        attrs.update(fields)
        attrs.update(self.get_extra_fields(model, fields))
        # type in python2 wants str as a first argument
        attrs.update(Meta=type(str('Meta'), (), self.get_meta_options(model)))
        name = 'Historical%s' % model._meta.object_name
        registered_models[model._meta.db_table] = model
        return python_2_unicode_compatible(
            type(str(name), (models.Model,), attrs))

    def copy_fields(self, model):
        """
        Creates copies of the model's original fields, returning
        a dictionary mapping field name to copied field object.
        """
        fields = {}
        for field in model._meta.fields:
            field = copy.copy(field)
            if isinstance(field, models.ForeignKey):
                # Don't allow reverse relations.
                # ForeignKey knows best what datatype to use for the column
                # we'll used that as soon as it's finalized by copying rel.to
                field.__class__ = get_custom_fk_class(type(field))
                field.rel.related_name = '+'
                field.null = True
                field.blank = True
            transform_field(field)
            fields[field.name] = field
        return fields

    def get_extra_fields(self, model, fields):
        """Return dict of extra fields added to the historical record model"""

        @models.permalink
        def revert_url(self):
            opts = model._meta
            return ('%s:%s_%s_simple_history' %
                    (admin.site.name, opts.app_label, opts.module_name),
                    [getattr(self, opts.pk.attname), self.history_id])

        def get_instance(self):
            return model(**dict([(k, getattr(self, k)) for k in fields]))

        rel_nm = '_%s_history' % model._meta.object_name.lower()

        return {
            'history_id': models.AutoField(primary_key=True),
            'history_date': models.DateTimeField(auto_now_add=True),
            'history_user': CurrentUserField(related_name=rel_nm),
            'history_type': models.CharField(max_length=1, choices=(
                ('+', 'Created'),
                ('~', 'Changed'),
                ('-', 'Deleted'),
            )),
            'history_object': HistoricalObjectDescriptor(model),
            'instance': property(get_instance),
            'revert_url': revert_url,
            '__str__': lambda self: '%s as of %s' % (self.history_object,
                                                     self.history_date)
        }

    def get_meta_options(self, model):
        """
        Returns a dictionary of fields that will be added to
        the Meta inner class of the historical record model.
        """
        return {
            'ordering': ('-history_date', '-history_id'),
        }

    def post_save(self, instance, created, **kwargs):
        if not created and hasattr(instance, 'skip_history_when_saving'):
            return
        if not kwargs.get('raw', False):
            self.create_historical_record(instance, created and '+' or '~')

    def post_delete(self, instance, **kwargs):
        self.create_historical_record(instance, '-')

    def create_historical_record(self, instance, type):
        manager = getattr(instance, self.manager_name)
        attrs = {}
        for field in instance._meta.fields:
            attrs[field.attname] = getattr(instance, field.attname)
        manager.create(history_type=type, **attrs)


def get_custom_fk_class(parent_type):
    class CustomForeignKey(parent_type):

        def get_attname(self):
            return self.name

        def do_related_class(self, other, cls):
            # this hooks into contribute_to_class() and this is
            # called specifically after the class_prepared signal
            to_field = copy.copy(self.rel.to._meta.pk)
            field = self
            if isinstance(to_field, models.AutoField):
                field.__class__ = models.IntegerField
            else:
                field.__class__ = to_field.__class__
                excluded_prefixes = ("_", "__")
                excluded_attributes = (
                    "rel",
                    "creation_counter",
                    "validators",
                    "error_messages",
                    "attname",
                    "column",
                    "help_text",
                    "name",
                    "model",
                    "unique_for_year",
                    "unique_for_date",
                    "unique_for_month",
                    "db_tablespace",
                    "db_index",
                    "db_column",
                    "default",
                    "auto_created",
                    "null",
                    "blank",
                )
                for key, val in to_field.__dict__.items():
                    if (isinstance(key, basestring)
                            and not key.startswith(excluded_prefixes)
                            and not key in excluded_attributes):
                        setattr(field, key, val)
            transform_field(field)
            field.rel = None

        def contribute_to_class(self, cls, name):
            # HACK: remove annoying descriptor (don't super())
            RelatedField.contribute_to_class(self, cls, name)

    return CustomForeignKey


def transform_field(field):
    """Customize field appropriately for use in historical model"""
    field.name = field.attname
    if isinstance(field, models.AutoField):
        # The historical model gets its own AutoField, so any
        # existing one must be replaced with an IntegerField.
        field.__class__ = models.IntegerField
    elif isinstance(field, models.FileField):
        # Don't copy file, just path.
        field.__class__ = models.TextField

    # Historical instance shouldn't change create/update timestamps
    field.auto_now = False
    field.auto_now_add = False

    if field.primary_key or field.unique:
        # Unique fields can no longer be guaranteed unique,
        # but they should still be indexed for faster lookups.
        field.primary_key = False
        field._unique = False
        field.db_index = True
        field.serialize = True


class HistoricalObjectDescriptor(object):
    def __init__(self, model):
        self.model = model

    def __get__(self, instance, owner):
        values = (getattr(instance, f.attname)
                  for f in self.model._meta.fields)
        return self.model(*values)
