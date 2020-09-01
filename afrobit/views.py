# -*- coding: utf-8 -*-
from django.views.generic import TemplateView


class GuardPage(TemplateView):
    template_name = 'shopping/guard_page.html'



