# -*- coding: utf-8 -*-
import logging
from datetime import timedelta, datetime
from threading import Thread

import requests
from currencies.conf import SESSION_KEY as CURRENCY_SESSION_KEY
from currencies.context_processors import currencies
from currencies.models import Currency
from daraja.models import Dara, Follower, DARA_CASH
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.urlresolvers import reverse
from django.db import transaction
from django.template.defaultfilters import slugify
from django.utils import timezone
from django.utils.translation import gettext as _, activate
from echo.models import Balance
from echo.utils import count_pages, notify_for_empty_messaging_credit, notify_for_low_messaging_credit, LOW_SMS_LIMIT
from ikwen.accesscontrol.backends import UMBRELLA
from ikwen.accesscontrol.models import SUDO, Member
from ikwen.billing.mtnmomo.views import MTN_MOMO
from ikwen.conf.settings import WALLETS_DB_ALIAS
from ikwen.core.models import Country, Service
from ikwen.core.utils import set_counters, get_service_instance, increment_history_field, get_mail_content, \
    add_database, add_event, send_sms, XEmailMessage
from ikwen_kakocase.kako.models import Product
from ikwen_kakocase.kako.utils import mark_duplicates
from ikwen_kakocase.kakocase.models import OperatorProfile, SOLD_OUT_EVENT, NEW_ORDER_EVENT
from ikwen_kakocase.kakocase.utils import set_customer_dara
from ikwen_kakocase.sales.models import PromoCode
from ikwen_kakocase.sales.views import apply_promotion_discount

from mediashop.models import Order as MediaOrder
from mediastore.models import Album, Song

logger = logging.getLogger('ikwen.crons')


def parse_order_info(request):
    from ikwen_kakocase.kako.models import Product
    from ikwen_kakocase.shopping.models import Customer, AnonymousBuyer, DeliveryAddress
    from ikwen_kakocase.trade.models import OrderEntry, Order
    from ikwen_kakocase.kakocase.models import DeliveryOption

    entries = request.POST['entries'].split(',')
    delivery_option_id = request.POST['delivery_option_id']
    buy_packing = request.POST.get('buy_packing', False)
    try:
        currency = Currency.objects.get(code=request.session[CURRENCY_SESSION_KEY])
    except KeyError:
        currency = Currency.active.base()
    except Currency.DoesNotExist:
        currency = Currency.objects.all()[0]

    delivery_option = DeliveryOption.objects.get(pk=delivery_option_id)
    seconds = delivery_option.max_delay * 3600
    deadline = timezone.now() + timedelta(seconds=seconds)
    pick_up_in_store = True if delivery_option.type == DeliveryOption.PICK_UP_IN_STORE else False
    order = Order(items_cost=0, items_count=0, total_cost=0, device=request.user_agent.device.family,
                  delivery_option=delivery_option, delivery_expected_on=deadline,
                  delivery_company=delivery_option.company, pick_up_in_store=pick_up_in_store,
                  currency=currency)

    previous_address_index = request.POST.get('previous_address_index')
    if not previous_address_index:
        try:
            country = Country.objects.get(iso2=request.POST.get('country_iso2'))
        except Country.DoesNotExist:
            country = None
        city = request.POST.get('city')
        name = request.POST.get('name')
        email = request.POST['email']
        phone = request.POST['phone']
        postal_code = request.POST.get('postal_code', 'N/A')
        if pick_up_in_store:
            details = delivery_option.name
        else:
            details = request.POST.get('details', '<empty>')
        address = DeliveryAddress(name=name, country=country, city=city,
                                  details=details, postal_code=postal_code, email=email, phone=phone)

    if request.user.is_authenticated():
        try:
            customer = Customer.objects.get(member=request.user)
        except Customer.DoesNotExist:
            customer = Customer.objects.create(member=request.user)
            customer.delivery_addresses.append(address)
        else:
            if not previous_address_index:
                for obj in customer.delivery_addresses:
                    if obj.country == country and obj.city == city and \
                            obj.details == details and obj.phone == phone and obj.email == email:
                        break
                else:
                    customer.delivery_addresses.append(address)
            else:
                address = customer.delivery_addresses[int(previous_address_index)]
        customer.save()
        order.tags = request.user.full_name
    else:
        anonymous_buyer_id = request.POST.get('anonymous_buyer_id')
        name = request.POST.get('name')
        order.tags = name
        if anonymous_buyer_id:
            try:
                anonymous_buyer = AnonymousBuyer.objects.get(pk=anonymous_buyer_id)
            except AnonymousBuyer.DoesNotExist:
                anonymous_buyer = AnonymousBuyer.objects.create(email=email, name=name, phone=phone)
        else:
            anonymous_buyer = AnonymousBuyer.objects.create(email=email, name=name, phone=phone)
        if not previous_address_index:
            try:
                last_address = anonymous_buyer.delivery_addresses[-1]
                if last_address.country != country or last_address.city != city or \
                        last_address.details != details or last_address.phone != phone or last_address.email != email:
                    anonymous_buyer.delivery_addresses.append(address)
            except IndexError:
                anonymous_buyer.delivery_addresses.append(address)
            anonymous_buyer.name = name
            anonymous_buyer.save()
        else:
            address = anonymous_buyer.delivery_addresses[int(previous_address_index)]
        order.anonymous_buyer = anonymous_buyer

    coupon = None
    if request.session.get('promo_code'):
        promo_id = request.session['promo_code_id']
        try:
            now = datetime.now()
            coupon = PromoCode.objects.get(pk=promo_id, start_on__lte=now, end_on__gt=now, is_active=True)
        except PromoCode.DoesNotExist:
            pass

    for entry in entries:
        tokens = entry.split(':')
        product = Product.objects.get(pk=tokens[0])
        product = apply_promotion_discount([product])[0]
        product.units_sold_history = []  # Wipe these unnecessary data for this case
        count = int(tokens[1])

        if not buy_packing:
            product.packing_price = 0  # Cancel packing price if refused to buy packing

        if coupon:
            rate = coupon.rate
            product.retail_price = product.retail_price * (100 - rate) / 100
            # product.packing_price = product.packing_price * (100 - rate) / 100

        order_entry = OrderEntry(product=product, count=count)
        order.entries.append(order_entry)
        order.items_count += count
        order.items_cost += product.retail_price * count
        order.packing_cost += product.packing_price * count
        order.tags += ' ' + product.name

    music_item_list = request.POST.get('music_items')
    album_list = []
    song_list = []
    media_total_cost = 0

    if music_item_list:
        music_item_id_list = music_item_list.split(',')
        for item_id in music_item_id_list:
            try:
                album = Album.objects.get(pk=item_id)
                album_list.append(album)
                media_total_cost += album.cost
            except:
                try:
                    song = Song.objects.get(pk=item_id)
                    song_list.append(song)
                    media_total_cost += song.cost
                except:
                    continue

    media_order = MediaOrder(member_id=request.user.id, album_list=album_list, song_list=song_list,
                             tags=order.id, total_cost=media_total_cost)

    order.coupon = coupon
    if buy_packing:
        order.packing_cost += delivery_option.packing_cost
    order.total_cost = order.items_cost + order.packing_cost + delivery_option.cost
    order.delivery_address = address
    return order, media_order


def send_order_confirmation_email(request, subject, buyer_name, buyer_email, order, media_order=None, message=None,
                                  reward_pack_list=None):
    service = get_service_instance()
    coupon_count = 0
    if reward_pack_list:
        template_name = 'shopping/mails/order_notice_with_reward.html'
        for pack in reward_pack_list:
            coupon_count += pack.count
    else:
        template_name = 'shopping/mails/order_notice.html'

    with transaction.atomic(using=WALLETS_DB_ALIAS):
        try:
            crcy = currencies(request)['CURRENCY']
            html_content = get_mail_content(subject, template_name=template_name,
                                            extra_context={'buyer_name': buyer_name, 'order': order, 'message': message,
                                                           'IS_BANK': getattr(settings, 'IS_BANK', False),
                                                           'coupon_count': coupon_count, 'crcy': crcy})
            sender = '%s <no-reply@%s>' % (service.project_name, service.domain)
            msg = XEmailMessage(subject, html_content, sender, [buyer_email])
            bcc = [email.strip() for email in service.config.notification_email.split(',') if email.strip()]
            delcom = order.delivery_option.company
            if service != delcom:
                db = delcom.database
                add_database(db)
                try:
                    delcom_config = OperatorProfile.objects.using(db).get(service=delcom)
                    bcc += [email.strip() for email in delcom_config.notification_email.split(',') if email.strip()]
                    bcc.append(delcom.member.email)
                except:
                    pass
            bcc.append(service.member.email)
            msg.bcc = list(set(bcc))
            msg.content_subtype = "html"
            Thread(target=lambda m: m.send(), args=(msg,)).start()
        except:
            logger.error("%s - Failed to send order confirmation email." % service, exc_info=True)


def send_dara_notification_email(dara_service, order):
    service = get_service_instance()
    config = service.config
    template_name = 'daraja/mails/new_transaction.html'

    activate(dara_service.member.language)
    subject = _("New transaction on %s" % config.company_name)
    try:
        dashboard_url = 'http://daraja.ikwen.com' + reverse('daraja:dashboard')
        extra_context = {'currency_symbol': config.currency_symbol, 'amount': order.items_cost,
                         'dara_earnings': order.referrer_earnings,
                         'tx_date': order.updated_on.strftime('%Y-%m-%d'),
                         'tx_time': order.updated_on.strftime('%H:%M:%S'),
                         'account_balance': dara_service.balance,
                         'dashboard_url': dashboard_url}
        try:
            dara = Dara.objects.using(UMBRELLA).get(member=dara_service.member)
            extra_context['dara'] = dara
        except:
            pass
        html_content = get_mail_content(subject, template_name=template_name, extra_context=extra_context)
        sender = 'ikwen Daraja <no-reply@ikwen.com>'
        msg = XEmailMessage(subject, html_content, sender, [dara_service.member.email])
        msg.content_subtype = "html"
        Thread(target=lambda m: m.send(), args=(msg,)).start()
    except:
        logger.error("Failed to notify %s Dara after follower purchase." % service, exc_info=True)


def send_order_confirmation_sms(buyer_name, buyer_phone, order):
    service = get_service_instance()
    config = service.config
    script_url = getattr(settings, 'SMS_API_SCRIPT_URL', config.sms_api_script_url)
    if not script_url:
        return
    details_max_length = 90
    details = order.get_products_as_string()
    if len(details) > details_max_length:
        tokens = details.split(',')
        while len(details) > details_max_length:
            tokens = tokens[:-1]
            details = ','.join(tokens)
        details += ' ...'
    client_text = _("Order successful:\n"
                    "%(details)s\n"
                    "Your RCC is %(rcc)s\n"
                    "Thank you." % {'details': details, 'rcc': order.rcc.upper()})
    iao_text = "Order from %(buyer_name)s:\n" \
               "%(details)s\n" \
               "RCC: %(rcc)s" % {'buyer_name': buyer_name[:20], 'details': details, 'rcc': order.rcc.upper()}

    iao_phones = [phone.strip() for phone in config.notification_phone.split(',') if phone.strip()]
    client_page_count = count_pages(client_text)
    iao_page_count = count_pages(iao_text)
    needed_credit = client_page_count + iao_page_count * len(iao_phones)
    balance, update = Balance.objects.using(WALLETS_DB_ALIAS).get_or_create(service_id=service.id)
    if needed_credit < balance.mail_count < LOW_SMS_LIMIT:
        try:
            notify_for_low_messaging_credit(service, balance)
        except:
            logger.error("Failed to notify %s for low messaging credit." % service, exc_info=True)
    if balance.sms_count < needed_credit:
        try:
            notify_for_empty_messaging_credit(service, balance)
        except:
            logger.error("Failed to notify %s for empty messaging credit." % service, exc_info=True)
        return
    buyer_phone = buyer_phone.strip()
    buyer_phone = slugify(buyer_phone).replace('-', '')
    if buyer_phone and len(buyer_phone) == 9:
        buyer_phone = '237' + buyer_phone  # This works only for Cameroon
    try:
        with transaction.atomic(using=WALLETS_DB_ALIAS):
            balance.sms_count -= client_page_count
            balance.save()
            send_sms(buyer_phone, client_text, script_url=script_url, fail_silently=False)
    except:
        pass

    for phone in iao_phones:
        phone = slugify(phone).replace('-', '')
        if len(phone) == 9:
            phone = '237' + phone
        try:
            with transaction.atomic(using=WALLETS_DB_ALIAS):
                balance.sms_count -= iao_page_count
                balance.save()
                send_sms(phone, iao_text, script_url=script_url, fail_silently=False)
        except:
            pass


def set_ikwen_earnings_and_stats(order):
    service = get_service_instance()
    service_umbrella = Service.objects.using(UMBRELLA).get(pk=service.id)
    app_umbrella = service_umbrella.app

    delcom = order.delivery_company
    service_delcom_umbrella = Service.objects.using(UMBRELLA).get(pk=delcom.id)
    app_delcom_umbrella = service_delcom_umbrella.app

    payment_mean_slug = order.payment_mean.slug
    if payment_mean_slug == DARA_CASH:
        payment_mean_slug = MTN_MOMO

    # IKWEN STATS
    set_counters(service_umbrella)
    increment_history_field(service_umbrella, 'turnover_history', order.items_cost)
    increment_history_field(service_umbrella, 'earnings_history', order.ikwen_order_earnings)
    increment_history_field(service_umbrella, 'transaction_count_history')
    increment_history_field(service_umbrella, 'transaction_earnings_history', order.ikwen_order_earnings)

    set_counters(app_umbrella)
    increment_history_field(app_umbrella, 'turnover_history', order.items_cost)
    increment_history_field(app_umbrella, 'earnings_history', order.ikwen_order_earnings)
    increment_history_field(app_umbrella, 'transaction_count_history')
    increment_history_field(app_umbrella, 'transaction_earnings_history', order.ikwen_order_earnings)

    set_counters(service_delcom_umbrella)
    increment_history_field(service_delcom_umbrella, 'turnover_history', order.delivery_option.cost)
    increment_history_field(service_delcom_umbrella, 'earnings_history', order.ikwen_delivery_earnings)
    increment_history_field(service_delcom_umbrella, 'transaction_count_history')
    increment_history_field(service_delcom_umbrella, 'transaction_earnings_history', order.ikwen_delivery_earnings)

    set_counters(app_delcom_umbrella)
    increment_history_field(app_delcom_umbrella, 'turnover_history', order.delivery_option.cost)
    increment_history_field(app_delcom_umbrella, 'earnings_history', order.ikwen_delivery_earnings)
    increment_history_field(app_delcom_umbrella, 'transaction_count_history')
    increment_history_field(app_delcom_umbrella, 'transaction_earnings_history', order.ikwen_delivery_earnings)

    partner = service.retailer
    if partner:
        service_partner = get_service_instance(partner.database)
        app_partner = service_partner.app
        partner_umbrella = Service.objects.using(UMBRELLA).get(pk=partner.id)
        partner_app_umbrella = partner_umbrella.app

        set_counters(service_partner)
        increment_history_field(service_partner, 'turnover_history', order.items_cost)
        increment_history_field(service_partner, 'earnings_history', order.eshop_partner_earnings)
        increment_history_field(service_partner, 'transaction_count_history')
        increment_history_field(service_partner, 'transaction_earnings_history', order.eshop_partner_earnings)

        set_counters(app_partner)
        increment_history_field(app_partner, 'turnover_history', order.items_cost)
        increment_history_field(app_partner, 'earnings_history', order.eshop_partner_earnings)
        increment_history_field(app_partner, 'transaction_count_history')
        increment_history_field(app_partner, 'transaction_earnings_history', order.eshop_partner_earnings)

        partner.raise_balance(order.eshop_partner_earnings, payment_mean_slug)

        set_counters(partner_umbrella)
        increment_history_field(partner_umbrella, 'turnover_history', order.items_cost)
        increment_history_field(partner_umbrella, 'earnings_history', order.ikwen_order_earnings)
        increment_history_field(partner_umbrella, 'transaction_count_history')
        increment_history_field(partner_umbrella, 'transaction_earnings_history', order.ikwen_order_earnings)

        set_counters(partner_app_umbrella)
        increment_history_field(partner_app_umbrella, 'turnover_history', order.items_cost)
        increment_history_field(partner_app_umbrella, 'earnings_history', order.ikwen_order_earnings)
        increment_history_field(partner_app_umbrella, 'transaction_count_history')
        increment_history_field(partner_app_umbrella, 'transaction_earnings_history', order.ikwen_order_earnings)

    delcom_partner = delcom.retailer  # Partner who actually created Delivery partner website
    if delcom_partner:
        service_delcom_partner = Service.objects.using(delcom_partner.database).get(pk=delcom.id)
        app_delcom_partner = service_delcom_partner.app
        delcom_partner_umbrella = Service.objects.using(UMBRELLA).get(pk=delcom_partner.id)
        delcom_partner_app_umbrella = delcom_partner_umbrella.app

        set_counters(service_delcom_partner)
        increment_history_field(service_delcom_partner, 'turnover_history', order.delivery_option.cost)
        increment_history_field(service_delcom_partner, 'earnings_history', order.logicom_partner_earnings)
        increment_history_field(service_delcom_partner, 'transaction_count_history')
        increment_history_field(service_delcom_partner, 'transaction_earnings_history', order.logicom_partner_earnings)

        set_counters(app_delcom_partner)
        increment_history_field(app_delcom_partner, 'turnover_history', order.delivery_option.cost)
        increment_history_field(app_delcom_partner, 'earnings_history', order.logicom_partner_earnings)
        increment_history_field(app_delcom_partner, 'transaction_count_history')
        increment_history_field(app_delcom_partner, 'transaction_earnings_history', order.logicom_partner_earnings)

        delcom_partner.raise_balance(order.logicom_partner_earnings, payment_mean_slug)

        set_counters(delcom_partner_umbrella)
        increment_history_field(delcom_partner_umbrella, 'turnover_history', order.delivery_option.cost)
        increment_history_field(delcom_partner_umbrella, 'earnings_history', order.ikwen_delivery_earnings)
        increment_history_field(delcom_partner_umbrella, 'transaction_count_history')
        increment_history_field(delcom_partner_umbrella, 'transaction_earnings_history', order.ikwen_delivery_earnings)

        set_counters(delcom_partner_app_umbrella)
        increment_history_field(delcom_partner_app_umbrella, 'turnover_history', order.delivery_option.cost)
        increment_history_field(delcom_partner_app_umbrella, 'earnings_history', order.ikwen_delivery_earnings)
        increment_history_field(delcom_partner_app_umbrella, 'transaction_count_history')
        increment_history_field(delcom_partner_app_umbrella, 'transaction_earnings_history',
                                order.ikwen_delivery_earnings)


def set_logicom_earnings_and_stats(order):
    delcom = order.delivery_company
    delcom_original = Service.objects.using(delcom.database).get(pk=delcom.id)
    delcom_profile_original = delcom_original.config
    provider = order.get_providers()[0]
    provider_profile_mirror = Service.objects.using(delcom.database).get(pk=provider.id).config
    payment_mean_slug = order.payment_mean.slug
    if payment_mean_slug == DARA_CASH:
        payment_mean_slug = MTN_MOMO

    set_counters(delcom_profile_original)
    increment_history_field(delcom_profile_original, 'items_traded_history', order.items_count)
    increment_history_field(delcom_profile_original, 'turnover_history', order.delivery_option.cost)
    increment_history_field(delcom_profile_original, 'orders_count_history')
    increment_history_field(delcom_profile_original, 'earnings_history', order.delivery_earnings)
    delcom_profile_original.report_counters_to_umbrella()

    delcom_original.raise_balance(order.delivery_earnings, payment_mean_slug)

    set_counters(provider_profile_mirror)
    increment_history_field(provider_profile_mirror, 'items_traded_history', order.items_count)
    increment_history_field(provider_profile_mirror, 'turnover_history', order.delivery_option.cost)
    increment_history_field(provider_profile_mirror, 'orders_count_history')
    increment_history_field(provider_profile_mirror, 'earnings_history', order.delivery_earnings)


def after_order_confirmation(order, media_order=None, update_stock=True):
    member = order.member
    service = get_service_instance()
    config = service.config
    delcom = order.delivery_option.company
    delcom_db = delcom.database
    add_database(delcom_db)
    sudo_group = Group.objects.get(name=SUDO)
    customer = member.customer
    packages_info = order.split_into_packages()

    for provider_db in packages_info.keys():
        package = packages_info[provider_db]['package']
        provider_earnings = package.provider_earnings
        raw_provider_revenue = package.provider_revenue
        provider_revenue = raw_provider_revenue
        if package.provider == delcom:
            provider_revenue += order.delivery_option.cost + order.delivery_option.packing_cost
            provider_earnings += order.delivery_earnings
        else:
            provider_revenue += order.delivery_option.packing_cost
            provider_earnings += order.delivery_option.packing_cost * (100 - config.ikwen_share_rate) / 100
        provider_profile_umbrella = packages_info[provider_db]['provider_profile']
        provider_profile_original = provider_profile_umbrella.get_from(provider_db)

        if provider_profile_original.return_url:
            nvp_dict = package.get_nvp_api_dict()
            Thread(target=lambda url, data: requests.post(url, data=data),
                   args=(provider_profile_original.return_url, nvp_dict)).start()

    if media_order:
        provider_revenue += media_order.total_cost
        provider_earnings += media_order.total_cost

    set_counters(config)
    increment_history_field(config, 'orders_count_history')
    increment_history_field(config, 'items_traded_history', order.items_count)
    increment_history_field(config, 'turnover_history', provider_revenue)
    increment_history_field(config, 'earnings_history', provider_earnings)

    set_counters(customer)
    customer.last_payment_on = datetime.now()
    increment_history_field(customer, 'orders_count_history')
    increment_history_field(customer, 'items_purchased_history', order.items_count)
    increment_history_field(customer, 'turnover_history', provider_revenue)
    increment_history_field(customer, 'earnings_history', provider_earnings)

    category_list = []
    for entry in order.entries:
        product = Product.objects.get(pk=entry.product.id)
        category = product.category

        turnover = entry.count * product.retail_price
        set_counters(category)
        provider_earnings = turnover
        increment_history_field(category, 'earnings_history', provider_earnings)
        increment_history_field(category, 'turnover_history', turnover)
        increment_history_field(category, 'items_traded_history', entry.count)
        if category not in category_list:
            increment_history_field(category, 'orders_count_history')
            category_list.append(category)

        if update_stock:
            product.stock -= entry.count
            if product.stock == 0:
                add_event(service, SOLD_OUT_EVENT, group_id=sudo_group.id, object_id=product.id)
                mark_duplicates(product)
        product.save()
        set_counters(product)
        increment_history_field(product, 'units_sold_history', entry.count)

    for album in media_order.album_list:
        set_counters(album)
        increment_history_field(album, 'earnings_history', album.cost)
        increment_history_field(album, 'turnover_history', album.cost)
        increment_history_field(album, 'units_sold_history')

    for song in media_order.song_list:
        if song.cost == 0:
            continue
        set_counters(song)
        increment_history_field(song, 'earnings_history', song.cost)
        increment_history_field(song, 'turnover_history', song.cost)
        increment_history_field(song, 'units_sold_history')


def referee_registration_callback(request, *args, **kwargs):
    """
    This function should run upon registration. This is achieved
    by adding its path to the IKWEN_REGISTER_EVENTS in settings file.
    This does necessary operations to bind a Dara to the Member newly login in.
    """
    referrer = request.COOKIES.get('referrer')
    if referrer:
        try:
            service = kwargs.get('service', get_service_instance())
            dara_member = Member.objects.get(pk=referrer)
            set_customer_dara(service, dara_member, request.user)
        except:
            pass
