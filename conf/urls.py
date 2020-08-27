from django.conf import settings
from django.conf.urls import patterns, include, url

from django.contrib import admin
from django.contrib.auth.decorators import permission_required, user_passes_test

from ikwen.core.views import Offline
from ikwen.core.analytics import analytics
from ikwen.accesscontrol.utils import is_staff, update_push_subscription
from ikwen_kakocase.shopping.views import FlatPageView

from ikwen_kakocase.trade.provider.views import ProviderDashboard, CCMDashboard
from ikwen_kakocase.kakocase.views import AdminHome, Welcome, GuardPage, FirstTime

from afrobit.views import MusicItemDetail, SongList, AfrobitHome, confirm_checkout

admin.autodiscover()

urlpatterns = patterns(
    '',
    url(r'^laakam/', include(admin.site.urls)),
    url(r'^kakocase/', include('ikwen_kakocase.kakocase.urls', namespace='kakocase')),
    url(r'^kako/', include('ikwen_kakocase.kako.urls', namespace='kako')),
    url(r'^trade/', include('ikwen_kakocase.trade.urls', namespace='trade')),
    url(r'^billing/', include('ikwen.billing.urls', namespace='billing')),
    url(r'^rewarding/', include('ikwen.rewarding.urls', namespace='rewarding')),
    url(r'^revival/', include('ikwen.revival.urls', namespace='revival')),
    url(r'^marketing/', include('ikwen_kakocase.commarketing.urls', namespace='marketing')),
    url(r'^sales/', include('ikwen_kakocase.sales.urls', namespace='sales')),
    url(r'^musicstore/', include('mediastore.urls', namespace='mediastore')),
    url(r'^music/', include('mediashop.urls', namespace='mediashop')),
    url(r'^music/songs$', SongList.as_view(), name='song_list'),
    url(r'^music/songs/(?P<slug>[-\w]+)$', SongList.as_view(), name='song_list'),
    url(r'^music/(?P<artist_slug>[-\w]+)/(?P<item_slug>[-\w]+)$', MusicItemDetail.as_view(), name='music_item_detail'),

    url(r'^echo/', include('echo.urls', namespace='echo')),
    url(r'^daraja/', include('daraja.urls', namespace='daraja')),

    url(r'^i18n/', include('django.conf.urls.i18n')),
    url(r'^currencies/', include('currencies.urls')),

    url(r'^ikwen/home/$', user_passes_test(is_staff)(AdminHome.as_view()), name='admin_home'),
    url(r'^ikwen/dashboard/$', permission_required('trade.ik_view_dashboard')(ProviderDashboard.as_view()), name='dashboard'),
    url(r'^ikwen/CCMDashboard/$', permission_required('trade.ik_view_dashboard')(CCMDashboard.as_view()), name='ccm_dashboard'),
    url(r'^ikwen/theming/', include('ikwen.theming.urls', namespace='theming')),
    url(r'^ikwen/cashout/', include('ikwen.cashout.urls', namespace='cashout')),
    url(r'^ikwen/cci/', include('ikwen_kakocase.cci.urls', namespace='cci')),
    url(r'^ikwen/', include('ikwen.core.urls', namespace='ikwen')),
    url(r'^analytics', analytics),
    url(r'^update_push_subscription$', update_push_subscription),

    url(r'^welcome/$', Welcome.as_view(), name='welcome'),
    url(r'^firstTime/$', FirstTime.as_view(), name='first_time'),
    url(r'^guardPage/$', GuardPage.as_view(), name='guard_page'),

    url(r'^page/(?P<url>[-\w]+)/$', FlatPageView.as_view(), name='flatpage'),
    url(r'^offline.html$', Offline.as_view(), name='offline'),
    url(r'^$', AfrobitHome.as_view(), name='home'),
    url(r'^', include('ikwen_kakocase.shopping.urls', namespace='shopping'))
)
