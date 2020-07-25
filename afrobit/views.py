# -*- coding: utf-8 -*-
import json
import logging
import random
import string
import time
from datetime import datetime, timedelta

import requests
from currencies.models import Currency
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.http.response import HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _, activate
from django.views.generic import TemplateView

from ikwen.conf.settings import WALLETS_DB_ALIAS
from ikwen.billing.models import MoMoTransaction
from ikwen.billing.mtnmomo.views import MTN_MOMO
from ikwen.core.constants import CONFIRMED
from ikwen.core.utils import get_service_instance, as_matrix
from ikwen.core.views import HybridListView
from ikwen.core.templatetags.url_utils import strip_base_alias

from ikwen_kakocase.kakocase.models import DeliveryOption
from ikwen_kakocase.kako.models import Product, Photo
from ikwen_kakocase.shopping.views import Home, TemplateSelector
from ikwen_kakocase.trade.models import Order, OrderEntry
from ikwen_kakocase.trade.utils import generate_tx_code

from mediastore.models import Artist, Album, Song
from mediashop.models import Order as MediaOrder
from mediashop.views import generate_download_link

from afrobit.utils import parse_order_info, send_order_confirmation_email, after_order_confirmation


logger = logging.getLogger('ikwen')

_OPTIMUM = 'optimum'
COZY = "Cozy"
COMPACT = "Compact"
COMFORTABLE = "Comfortable"


class AfrobitHome(Home):

    def get_context_data(self, **kwargs):
        context = super(AfrobitHome, self).get_context_data(**kwargs)
        album_list = list(Album.objects.select_related('artist')
                          .filter(is_active=True, show_on_home=True).order_by('order_of_appearance'))
        song_list = list(Song.objects.select_related('artist', 'album')
                         .filter(is_active=True, show_on_home=True).order_by('order_of_appearance'))
        media_list = album_list + song_list
        media_matrix = as_matrix(media_list, self._get_row_len(), strict=True)
        context['media_matrix'] = media_matrix
        return context


class AlbumList(HybridListView):
    template_name = 'shopping/optimum/music_item_detail.html'

    def get_context_data(self, **kwargs):
        context = super(AlbumList, self).get_context_data(**kwargs)
        slug = kwargs.get('slug')
        if slug:
            artist = get_object_or_404(Artist, slug=slug)
            album_list = Album.objects.select_related('artist').filter(artist=artist).order_by('-id')
            song_list = Song.objects.select_related('artist').filter(artist=artist, album=None).order_by('-id')
        else:
            album_list = Album.objects.select_related('artist').all().order_by('-id')[:20]
            song_list = Song.objects.select_related('artist').filter(album=None)[:20].order_by('-id')
        context['album_list'] = album_list
        context['song_list'] = song_list
        return context


class SongList(HybridListView):
    template_name = 'shopping/optimum/song_list.html'
    html_results_template_name = 'shopping/snippets/song_list_results.html'

    def _get_row_len(self):
        config = get_service_instance().config
        if config.theme and config.theme.display == COMPACT:
            return 3
        return 2

    def get_queryset(self):
        slug = self.kwargs.get('slug')
        if slug:
            artist = get_object_or_404(Artist, slug=slug)
            queryset = Song.objects.select_related('artist').filter(artist=artist, album=None).order_by('-id')
        else:
            queryset = Song.objects.select_related('artist').filter(is_active=True)
        return queryset

    def get_context_data(self, **kwargs):
        context = super(SongList, self).get_context_data(**kwargs)
        slug = kwargs.get('slug')
        queryset = context['object_list']
        page_size = 9 if self.request.user_agent.is_mobile else 15
        if slug:
            artist = get_object_or_404(Artist, slug=slug)
            album_list = Album.objects.select_related('artist').filter(artist=artist).order_by('-id')
            context['album_list'] = album_list
        paginator = Paginator(queryset, page_size)
        products_page = paginator.page(1)
        context['products_page'] = products_page
        context['product_list_as_matrix'] = as_matrix(products_page.object_list, self._get_row_len())
        return context

    def render_to_response(self, context, **response_kwargs):
        if self.request.GET.get('format') == 'html_results':
            page_size = 9 if self.request.user_agent.is_mobile else 15
            queryset = self.get_queryset()
            product_queryset = self.get_search_results(queryset, max_chars=4)
            paginator = Paginator(product_queryset, page_size)
            page = self.request.GET.get('page')
            try:
                products_page = paginator.page(page)
                context['product_list_as_matrix'] = as_matrix(products_page.object_list, self._get_row_len())
            except PageNotAnInteger:
                products_page = paginator.page(1)
                context['product_list_as_matrix'] = as_matrix(products_page.object_list, self._get_row_len())
            except EmptyPage:
                products_page = paginator.page(paginator.num_pages)
                context['product_list_as_matrix'] = as_matrix(products_page.object_list, self._get_row_len())
            context['products_page'] = products_page
            return render(self.request, 'shopping/snippets/product_list_results.html', context)
        else:
            return super(SongList, self).render_to_response(context, **response_kwargs)


class MusicItemDetail(TemplateView):
    template_name = 'shopping/optimum/music_item_detail.html'

    def get_context_data(self, **kwargs):
        context = super(MusicItemDetail, self).get_context_data(**kwargs)
        artist_slug = kwargs.get('artist_slug')
        item_slug = kwargs.get('item_slug')
        artist = get_object_or_404(Artist, slug=artist_slug)
        try:
            item = Album.objects.select_related('artist').get(artist=artist, slug=item_slug, is_active=True)
        except Album.DoesNotExist:
            try:
                item = Song.objects.select_related('artist', 'album').get(artist=artist, slug=item_slug, is_active=True)
            except:
                raise Http404("No such item found")
        context['product'] = item
        return context


class Cart(TemplateSelector, TemplateView):
    template_name = 'shopping/cart.html'
    optimum_template_name = 'shopping/optimum/cart.html'

    def get_context_data(self, **kwargs):
        context = super(Cart, self).get_context_data(**kwargs)
        try:
            max_delivery_packing_cost = DeliveryOption.objects.filter(is_active=True).order_by('-packing_cost')[0].packing_cost
        except IndexError:
            max_delivery_packing_cost = 0
        context['max_delivery_packing_cost'] = max_delivery_packing_cost
        order_id = kwargs.get('order_id')
        if order_id:
            order = get_object_or_404(Order, pk=order_id)
            try:
                media_order = MediaOrder.objects.get(tags=order_id)
                order.items_count += len(media_order.album_list)
                for album in media_order.album_list:
                    product = Product(name=album.title, retail_price=album.cost, photos=[Photo(image=album.image)])
                    entry = OrderEntry(product=product, count=1)
                    order.entries.append(entry)
                for song in media_order.song_list:
                    if song.cost > 0:  # Song with cost == 0 are songs purchased within an album
                        product = Product(name=song.title, retail_price=song.cost, photos=[Photo(image=song.image)])
                        entry = OrderEntry(product=product, count=1)
                        order.entries.append(entry)
                        order.items_count += 1
                order.items_cost += media_order.total_cost
                order.download_id = media_order.id
            except:
                pass
            if order.status != Order.PENDING_FOR_PAYMENT:
                if not order.currency:
                    order.currency = Currency.active.base()
                diff = datetime.now() - order.created_on
                if diff.total_seconds() >= 3600:
                    order.is_more_than_one_hour_old = True
                context['order'] = order

            self.request.session.modified = True
            try:
                del self.request.session['promo_code']
            except:
                pass
        return context


def set_momo_order_checkout(request, payment_mean, *args, **kwargs):
    """
    This function has no URL associated with it.
    It serves as ikwen setting "MOMO_BEFORE_CHECKOUT"
    """
    service = get_service_instance()
    config = service.config
    if getattr(settings, 'DEBUG', False):
        order, media_order = parse_order_info(request)
    else:
        try:
            order, media_order = parse_order_info(request)
        except:
            return HttpResponseRedirect(reverse('shopping:checkout'))
    order.retailer = service
    order.payment_mean = payment_mean
    order.save()  # Save first to generate the Order id
    order = Order.objects.get(pk=order.id)  # Grab the newly created object to avoid create another one in subsequent save()
    media_order.tags = order.id
    media_order.save()

    member = request.user
    if member.is_authenticated():
        order.member = member
    else:
        order.aotc = generate_tx_code(order.id, order.anonymous_buyer.auto_inc)

    order.rcc = generate_tx_code(order.id, config.rel_id)
    order.save()
    signature = ''.join([random.SystemRandom().choice(string.ascii_letters + string.digits) for i in range(16)])

    if getattr(settings, 'UNIT_TESTING', False) and payment_mean.slug != 'mtn-momo':
        request.session['object_id'] = order.id
        request.session['signature'] = signature

    amount = order.total_cost
    model_name = 'trade.Order'
    mean = request.GET.get('mean', MTN_MOMO)
    cancel_url = reverse('shopping:cart')

    tx = MoMoTransaction.objects.using(WALLETS_DB_ALIAS)\
        .create(service_id=service.id, type=MoMoTransaction.CASH_OUT, amount=amount, phone='N/A', model=model_name,
                object_id=order.id, task_id=signature, wallet=mean, username=request.user.username, is_running=True)

    notification_url = reverse('shopping:confirm_checkout', args=(tx.id, signature))
    return_url = reverse('shopping:cart', args=(order.id, ))

    if getattr(settings, 'UNIT_TESTING', False):
        return HttpResponse(json.dumps({'notification_url': notification_url}), content_type='text/json')
    gateway_url = getattr(settings, 'IKWEN_PAYMENT_GATEWAY_URL', 'http://payment.ikwen.com/v1')
    endpoint = gateway_url + '/request_payment'
    params = {
        'username': getattr(settings, 'IKWEN_PAYMENT_GATEWAY_USERNAME', service.project_name_slug),
        'amount': order.total_cost,
        'merchant_name': config.company_name,
        'notification_url': service.url + strip_base_alias(notification_url),
        'return_url': service.url + strip_base_alias(return_url),
        'cancel_url': service.url + strip_base_alias(cancel_url),
        'user_id': request.user.username
    }
    try:
        r = requests.get(endpoint, params, verify=False)
        resp = r.json()
        token = resp.get('token')
        if token:
            next_url = gateway_url + '/checkoutnow/' + resp['token'] + '?mean=' + mean
        else:
            logger.error("%s - Init payment flow failed with URL %s and message %s" % (service.project_name, r.url, resp['errors']))
            messages.error(request, resp['errors'])
            next_url = cancel_url
    except:
        logger.error("%s - Init payment flow failed with URL." % service.project_name, exc_info=True)
        next_url = cancel_url
    return HttpResponseRedirect(next_url)


def confirm_checkout(request, *args, **kwargs):
    order_id = request.POST.get('order_id', request.session.get('object_id'))
    if order_id:
        signature = request.session['signature']
    else:
        status = request.GET['status']
        message = request.GET['message']
        operator_tx_id = request.GET['operator_tx_id']
        phone = request.GET['phone']
        tx_id = kwargs['tx_id']
        try:
            tx = MoMoTransaction.objects.using(WALLETS_DB_ALIAS).get(pk=tx_id)
            if not getattr(settings, 'DEBUG', False):
                tx_timeout = getattr(settings, 'IKWEN_PAYMENT_GATEWAY_TIMEOUT', 15) * 60
                expiry = tx.created_on + timedelta(seconds=tx_timeout)
                if datetime.now() > expiry:
                    return HttpResponse("Transaction %s timed out." % tx_id)

            tx.status = status
            tx.message = 'OK' if status == MoMoTransaction.SUCCESS else message
            tx.processor_tx_id = operator_tx_id
            tx.phone = phone
            tx.is_running = False
            tx.save()
        except:
            raise Http404("Transaction %s not found" % tx_id)
        if status != MoMoTransaction.SUCCESS:
            return HttpResponse("Notification for transaction %s received with status %s" % (tx_id, status))
        order_id = tx.object_id
        signature = tx.task_id

    callback_signature = kwargs.get('signature')
    no_check_signature = request.GET.get('ncs')
    if getattr(settings, 'DEBUG', False):
        if not no_check_signature:
            if callback_signature != signature:
                return HttpResponse('Invalid transaction signature')
    else:
        if callback_signature != signature:
            return HttpResponse('Invalid transaction signature')

    order = get_object_or_404(Order, pk=order_id)

    order.status = Order.PENDING
    order.save()

    timeout = getattr(settings, 'SECURE_LINK_TIMEOUT', 90)
    expires = int(time.time()) + timeout * 60
    song_list = []
    while True:
        try:
            MediaOrder.objects.get(expires=expires)
            expires += 1
        except:
            break

    media_order = MediaOrder.objects.get(tags=order.id)
    media_order.expires = expires
    for album in media_order.album_list:
        for song in album.song_set.all():
            filename = song.media.name
            song.cost = 0  # This helps now that the song was not purchased individually
            song.download_link = generate_download_link(filename, expires)
            song_list.append(song)

    for song in media_order.song_list:
        filename = song.media.name
        song.download_link = generate_download_link(filename, expires)
        song_list.append(song)
    media_order.song_list = song_list
    media_order.status = CONFIRMED
    media_order.save()

    after_order_confirmation(order, media_order)
    member = order.member
    buyer_name = member.full_name
    buyer_email = order.delivery_address.email

    activate(member.language)
    subject = _("Order successful")
    send_order_confirmation_email(request, subject, buyer_name, buyer_email, order, media_order)

    return HttpResponse("Notification received")


