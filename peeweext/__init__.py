import json

import datetime
import peewee as pw
import pendulum
from blinker import signal
from google.protobuf.json_format import ParseDict
from playhouse import pool, db_url, shortcuts, dataset

from .utils import cast_dict

__version__ = '0.2.0'


class DatetimeTZField(pw.Field):
    field_type = 'DATETIME'

    def python_value(self, value):
        if isinstance(value, str):
            return pendulum.parse(value)
        if isinstance(value, datetime.datetime):
            return pendulum.instance(value)
        return value

    def db_value(self, value):
        if value is None:
            return value
        if not isinstance(value, datetime.datetime):
            raise ValueError('datetime instance required')
        if value.utcoffset() is None:
            raise ValueError('timezone aware datetime required')
        if isinstance(value, pendulum.Pendulum):
            value = value._datetime
        return value.astimezone(datetime.timezone.utc)


pre_save = signal('pre_save')
post_save = signal('post_save')
pre_delete = signal('pre_delete')
post_delete = signal('post_delete')
pre_init = signal('pre_init')


class Model(pw.Model):
    class Meta:
        # see playhouse.shortcuts.model_to_dict
        model_to_dict_config = {
            'recurse': True,
            'backrefs': False,
            'only': True,
            'exclude': True,
            'seen': True,
            'extra_attrs': None,
            'fields_from_query': None,
            'max_depth': None,
            'manytomany': False
        }
        message_class = None

    created_at = DatetimeTZField(default=pendulum.utcnow)
    updated_at = DatetimeTZField(default=pendulum.utcnow)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pre_init.send(type(self), instance=self)

    def save(self, *args, **kwargs):
        pk_value = self._pk
        created = kwargs.get('force_insert', False) or not bool(pk_value)
        pre_save.send(type(self), instance=self, created=created)
        ret = super().save(*args, **kwargs)
        post_save.send(type(self), instance=self, created=created)
        return ret

    def delete_instance(self, *args, **kwargs):
        pre_delete.send(type(self), instance=self)
        ret = super().delete_instance(*args, **kwargs)
        post_delete.send(type(self), instance=self)
        return ret

    def update_from_dict(self, data, ignore_unknown=False):
        return shortcuts.update_model_from_dict(self, data, ignore_unknown)

    def to_dict(self, default=dataset.JSONExporter.default, **kwargs):
        default_kwargs = self._meta.serialize_config.copy()
        default_kwargs.update(kwargs)
        d = shortcuts.model_to_dict(self, **self._meta.default_kwargs)
        if default:
            d = cast_dict(d, default)
        return d

    def to_json(self, **kwargs):
        return json.dumps(self.to_dict(), **kwargs)

    def to_message(self, message_class=None, ignore_unknown_fields=False):
        return ParseDict(
            self.to_dict(),
            message_class or self._meta.message_class,
            ignore_unknown_fields
        )


def _touch_model(sender, instance, created):
    if issubclass(sender, Model):
        instance.updated_at = pendulum.utcnow()


pre_save.connect(_touch_model)
pw.MySQLDatabase.field_types.update({'DATETIME': 'DATETIME(6)'})
pw.PostgresqlDatabase.field_types.update({'DATETIME': 'TIMESTAMPTZ'})


class SmartDatabase:
    """
    if you use transaction, you must wrap it with a connection context explict:

    with db.connection_context():
        with db.atomic() as transaction:
            pass

    **notice**: if you use nested transactions, only wrap the most outside one
    """

    def execute(self, *args, **kwargs):
        if self.in_transaction():
            return super().execute(*args, **kwargs)
        with self.connection_context():
            return super().execute(*args, **kwargs)


_smarts = {
    'SmartMySQLDatabase': ['mysql+smart'],
    'SmartPostgresqlDatabase': ['postgres+smart', 'postgresql+smart'],
    'SmartPostgresqlExtDatabase': ['postgresext+smart', 'postgresqlext+smart'],
    'SmartSqliteDatabase': ['sqlite+smart'],
    'SmartSqliteExtDatabase': ['sqliteext+smart'],
    'SmartCSqliteExtDatabase': ['csqliteext+smart']
}

for n, urls in _smarts.items():
    pc = getattr(pool, 'Pooled{}'.format(n[5:]))
    if pc is not None:
        smart_cls = type(n, (SmartDatabase, pc), {})
        db_url.register_database(smart_cls, *urls)
        globals()[n] = smart_cls
