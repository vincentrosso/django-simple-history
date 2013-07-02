__author__ = 'Steven Ogdahl'

from django.db.models import signals
from django.utils.functional import curry
from django.utils.decorators import decorator_from_middleware

from registration import FieldRegistry

class CurrentUserMiddleware(object):

    def process_request(self, request):
        if request.method in ['GET', 'HEAD', 'OPTIONS', 'TRACE']:
            # We aren't doing anything return..
            return

        if hasattr(request, 'user') and request.user.is_authenticated():
            user = request.user
        else:
            user = None

        update_users = curry(self.update_users, user)
        signals.pre_save.connect(update_users, dispatch_uid=request, weak=False)

    def update_users(self, user, sender, instance, **kwargs):
        # We need to make sure the user is authenticated first (e.g. not a SimpleLazyObject)
        if user.is_authenticated():
            registry = FieldRegistry()
            if sender in registry:
                for field in registry.get_fields(sender):
                    setattr(instance, field.name, user)

    def process_response(self, request, response):
        signals.pre_save.disconnect(dispatch_uid=request)
        return response

record_current_user = decorator_from_middleware(CurrentUserMiddleware)