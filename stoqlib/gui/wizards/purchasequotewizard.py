# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2008 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s): Stoq Team <stoq-devel@async.com.br>
##
""" Purchase quote wizard definition """

import datetime
from decimal import Decimal

import gtk
from kiwi.currency import currency
from kiwi.datatypes import ValidationError
from kiwi.python import Settable
from kiwi.ui.objectlist import Column

from stoqlib.api import api
from stoqlib.domain.payment.group import PaymentGroup
from stoqlib.domain.person import Branch
from stoqlib.domain.purchase import (PurchaseOrder, PurchaseItem, QuoteGroup,
                                     Quotation)
from stoqlib.domain.sellable import Sellable
from stoqlib.domain.views import QuotationView
from stoqlib.gui.base.dialogs import run_dialog
from stoqlib.gui.base.lists import SimpleListDialog
from stoqlib.gui.base.wizards import (WizardEditorStep, BaseWizard,
                                      BaseWizardStep)
from stoqlib.gui.dialogs.quotedialog import QuoteFillingDialog
from stoqlib.gui.editors.purchaseeditor import PurchaseQuoteItemEditor
from stoqlib.gui.search.searchcolumns import IdentifierColumn
from stoqlib.gui.search.searchfilters import DateSearchFilter
from stoqlib.gui.search.searchslave import SearchSlave
from stoqlib.gui.utils.printing import print_report
from stoqlib.gui.wizards.purchasewizard import (PurchaseItemStep,
                                                PurchaseWizard)
from stoqlib.lib.dateutils import localtoday
from stoqlib.lib.message import info, yesno
from stoqlib.lib.parameters import sysparam
from stoqlib.lib.translation import stoqlib_gettext
from stoqlib.lib.formatters import format_quantity, get_formatted_cost
from stoqlib.reporting.purchase import PurchaseQuoteReport

_ = stoqlib_gettext


#
# Wizard Steps
#


class StartQuoteStep(WizardEditorStep):
    gladefile = 'StartQuoteStep'
    model_type = PurchaseOrder
    proxy_widgets = ['open_date', 'quote_deadline', 'branch_combo', 'notes']

    def __init__(self, wizard, previous, store, model):
        WizardEditorStep.__init__(self, store, wizard, model, previous)

    def _setup_widgets(self):
        quote_group = str(self.wizard.quote_group.identifier)
        self.quote_group.set_text(quote_group)

        branches = Branch.get_active_branches(self.store)
        self.branch_combo.prefill(api.for_person_combo(branches))
        sync_mode = api.sysparam.get_bool('SYNCHRONIZED_MODE')
        self.branch_combo.set_sensitive(not sync_mode)

        self.notes.set_accepts_tab(False)

    def post_init(self):
        self.register_validate_function(self.wizard.refresh_next)
        self.force_validation()

    def next_step(self):
        return QuoteItemStep(self.wizard, self, self.store, self.model)

    #
    # BaseEditorSlave
    #

    def setup_proxies(self):
        self._setup_widgets()
        self.add_proxy(self.model, StartQuoteStep.proxy_widgets)

    #
    # Kiwi Callbacks
    #

    def on_quote_deadline__validate(self, widget, date):
        if date < localtoday().date():
            return ValidationError(_(u"The quote deadline date must be set to "
                                     "today or a future date"))


class QuoteItemStep(PurchaseItemStep):
    item_editor = PurchaseQuoteItemEditor

    def get_sellable_view_query(self):
        query = Sellable.get_unblocked_sellables_query(self.store)
        return self.sellable_view, query

    def setup_slaves(self):
        PurchaseItemStep.setup_slaves(self)
        self.cost_label.hide()
        self.cost.hide()

    def get_order_item(self, sellable, cost, quantity, batch=None, parent=None):
        assert batch is None
        item = self.model.add_item(sellable, quantity)
        # since we are quoting products, it should not have
        # predefined cost. It should be filled later, when the
        # supplier reply our quoting request.
        item.cost = currency(0)
        return item

    def get_columns(self):
        return [
            Column('sellable.description', title=_('Description'),
                   data_type=str, expand=True, searchable=True),
            Column('quantity', title=_('Quantity'), data_type=float, width=90,
                   format_func=format_quantity),
            Column('sellable.unit_description', title=_('Unit'), data_type=str,
                   width=70),
        ]

    def _setup_summary(self):
        # disables summary label for the quoting list
        self.summary = False

    #
    # WizardStep
    #

    def validate(self, value):
        PurchaseItemStep.validate(self, value)
        can_quote = not self.model.get_items().is_empty()
        self.wizard.refresh_next(value and can_quote)

    def post_init(self):
        PurchaseItemStep.post_init(self)

        if not self.has_next_step():
            self.wizard.enable_finish()

    def has_next_step(self):
        # if we are editing a quote, this is the first and last step
        return not self.wizard.edit

    def next_step(self):
        return QuoteSupplierStep(self.wizard, self, self.store, self.model)


class QuoteSupplierStep(WizardEditorStep):
    gladefile = 'QuoteSupplierStep'
    model_type = PurchaseOrder

    # Class attribute so we can test it easier
    product_columns = [
        Column('description', title=_(u'Product'), data_type=str,
               expand=True)]

    def __init__(self, wizard, previous, store, model):
        WizardEditorStep.__init__(self, store, wizard, model, previous)
        self._setup_widgets()

    def _setup_widgets(self):
        self.quoting_list.set_columns(self._get_columns())
        self._populate_quoting_list()

        if not len(self.quoting_list) > 0:
            info(_(u'No supplier have been found for any of the selected '
                   'items.\nThis quote will be cancelled.'))
            self.wizard.finish()

    def _get_columns(self):
        return [Column('selected', title=" ", data_type=bool, editable=True),
                Column('supplier.person.name', title=_('Supplier'),
                       data_type=str, sorted=True, expand=True),
                Column('products_per_supplier', title=_('Supplied/Total'),
                       data_type=str)]

    def _update_widgets(self):
        selected = self.quoting_list.get_selected()
        self.print_button.set_sensitive(selected is not None)
        self.view_products_button.set_sensitive(selected is not None)

    def _populate_quoting_list(self):
        # populate the quoting list by finding the suppliers based on the
        # products list
        quotes = {}
        total_items = 0
        # O(n*n)
        for item in self.model.get_items():
            total_items += 1
            sellable = item.sellable
            product = sellable.product
            for supplier_info in product.suppliers:
                supplier = supplier_info.supplier
                if supplier is None:
                    continue

                if supplier not in quotes.keys():
                    quotes[supplier] = [sellable]
                else:
                    quotes[supplier].append(sellable)

        for supplier, items in quotes.items():
            total_supplier_items = len(items)
            per_supplier = _(u"%s/%s") % (total_supplier_items, total_items)
            self.quoting_list.append(Settable(supplier=supplier,
                                     items=items,
                                     products_per_supplier=per_supplier,
                                     selected=True))

    def _print_quote(self):
        selected = self.quoting_list.get_selected()
        self.model.supplier = selected.supplier
        print_report(PurchaseQuoteReport, self.model)

    def _generate_quote(self, selected):
        # we use our model as a template to create new quotes
        quote = self.model.clone()
        # we need to overwrite some values:
        quote.group = PaymentGroup(store=self.store)

        include_all = self.include_all_products.get_active()
        for item in self.model.get_items():
            if item.sellable in selected.items or include_all:
                quote_item = item.clone()
                quote_item.order = quote

        quote.supplier = selected.supplier
        self.wizard.quote_group.add_item(quote)
        self.wizard.quote = quote

        self.store.commit()

    def _show_products(self):
        selected = self.quoting_list.get_selected()
        title = _(u'Products supplied by %s') % selected.supplier.person.name
        run_dialog(SimpleListDialog, self.wizard, self.product_columns,
                   selected.items, title=title)

    def _show_missing_products(self):
        missing_products = set([i.sellable for i in self.model.get_items()])
        for quote in self.quoting_list:
            if quote.selected:
                missing_products = missing_products.difference(quote.items)
            if len(missing_products) == 0:
                break

        run_dialog(SimpleListDialog, self.wizard, self.product_columns,
                   missing_products, title=_(u'Missing Products'))

    def _update_wizard(self):
        # we need at least one supplier to finish this wizard
        can_finish = any([i.selected for i in self.quoting_list])
        self.wizard.refresh_next(can_finish)

    #
    # WizardStep hooks
    #

    def validate_step(self):
        # I am using validate_step as a callback for the finish button
        for item in self.quoting_list:
            if item.selected:
                self._generate_quote(item)

        return True

    def has_next_step(self):
        return False

    def post_init(self):
        self.register_validate_function(self.wizard.refresh_next)
        self.force_validation()

    #
    # Kiwi Callbacks
    #

    def on_print_button__clicked(self, widget):
        self._print_quote()

    def on_missing_products_button__clicked(self, widget):
        self._show_missing_products()

    def on_view_products_button__clicked(self, widget):
        self._show_products()

    def on_quoting_list__selection_changed(self, widget, item):
        self._update_widgets()

    def on_quoting_list__cell_edited(self, widget, item, cell):
        self._update_wizard()

    def on_quoting_list__row_activated(self, widget, item):
        self._show_products()


class QuoteGroupSelectionStep(BaseWizardStep):
    gladefile = 'QuoteGroupSelectionStep'

    def __init__(self, wizard, store):
        self._next_step = None
        BaseWizardStep.__init__(self, store, wizard)
        self._setup_slaves()

    def _setup_slaves(self):
        self.search = SearchSlave(self._get_columns(),
                                  restore_name=self.__class__.__name__,
                                  search_spec=QuotationView,
                                  store=self.store)
        self.attach_slave('search_group_holder', self.search)

        self.search.set_text_field_columns(['supplier_name', 'identifier_str'])
        filter = self.search.get_primary_filter()
        filter.set_label(_(u'Supplier:'))
        self.search.focus_search_entry()
        self.search.results.connect('selection-changed',
                                    self._on_searchlist__selection_changed)
        self.search.results.connect('row-activated',
                                    self._on_searchlist__row_activated)

        date_filter = DateSearchFilter(_('Date:'))
        self.search.add_filter(date_filter, columns=['open_date', 'deadline'])

        self.edit_button.set_sensitive(False)
        self.remove_button.set_sensitive(False)

    def _get_columns(self):
        return [IdentifierColumn('identifier', title=_("Quote #"), sorted=True),
                IdentifierColumn('group_identifier', title=_('Group #')),
                Column('supplier_name', title=_('Supplier'), data_type=str,
                       width=300),
                Column('open_date', title=_('Open date'),
                       data_type=datetime.date),
                Column('deadline', title=_('Deadline'),
                       data_type=datetime.date)]

    def _can_purchase(self, item):
        return item.cost > currency(0) and item.quantity > Decimal(0)

    def _can_order(self, quotation):
        if quotation is None:
            return False

        for item in quotation.purchase.get_items():
            if not self._can_purchase(item):
                return False
        return True

    def _update_view(self):
        selected = self.search.results.get_selected()
        has_selected = selected is not None
        self.edit_button.set_sensitive(has_selected)
        self.remove_button.set_sensitive(has_selected)
        self.wizard.refresh_next(self._can_order(selected))

    def _run_quote_editor(self):
        store = api.new_store()
        selected = store.fetch(self.search.results.get_selected().purchase)
        retval = run_dialog(QuoteFillingDialog, self.wizard, selected, store)
        store.confirm(retval)
        store.close()
        self._update_view()

    def _remove_quote(self):
        q = self.search.results.get_selected().quotation
        msg = _('Are you sure you want to remove "%s" ?') % q.get_description()
        if not yesno(msg, gtk.RESPONSE_NO,
                     _("Remove quote"), _("Don't remove")):
            return

        store = api.new_store()
        group = store.fetch(q.group)
        quote = store.fetch(q)
        group.remove_item(quote)
        # there is no reason to keep the group if there's no more quotes
        if group.get_items().count() == 0:
            store.remove(group)
        store.confirm(True)
        store.close()
        self.search.refresh()

    #
    # WizardStep hooks
    #

    def next_step(self):
        self.search.save_columns()
        selected = self.search.results.get_selected()
        if selected is None:
            return

        return QuoteGroupItemsSelectionStep(self.wizard, self.store,
                                            selected.group, self)

    #
    # Callbacks
    #

    def _on_searchlist__selection_changed(self, widget, item):
        self._update_view()

    def _on_searchlist__row_activated(self, widget, item):
        self._run_quote_editor()

    def on_edit_button__clicked(self, widget):
        self._run_quote_editor()

    def on_remove_button__clicked(self, widget):
        self._remove_quote()


class QuoteGroupItemsSelectionStep(BaseWizardStep):
    gladefile = 'QuoteGroupItemsSelectionStep'

    def __init__(self, wizard, store, group, previous=None):
        self._group = group
        self._next_step = None
        BaseWizardStep.__init__(self, store, wizard, previous)
        self._setup_widgets()

    def _setup_widgets(self):
        self.quoted_items.connect(
            'selection-changed', self._on_quoted_items__selection_changed)
        self.quoted_items.set_columns(self._get_columns())
        # populate the list
        for quote in self._group.get_items():
            for purchase_item in quote.purchase.get_items():
                if not self._can_purchase(purchase_item):
                    continue

                sellable = purchase_item.sellable
                ordered_qty = \
                    PurchaseItem.get_ordered_quantity(self.store, sellable)

                self.quoted_items.append(Settable(
                    selected=True, order=quote.purchase, item=purchase_item,
                    description=sellable.get_description(),
                    supplier=quote.purchase.supplier_name,
                    quantity=purchase_item.quantity,
                    ordered_quantity=ordered_qty,
                    cost=purchase_item.cost))

    def _get_columns(self):
        return [Column('selected', title=" ", data_type=bool, editable=True),
                Column('description', title=_('Description'), data_type=str,
                       expand=True, sorted=True),
                Column('supplier', title=_('Supplier'), data_type=str,
                       expand=True),
                Column('quantity', title=_(u'Quantity'), data_type=Decimal),
                Column('ordered_quantity', title=_(u'Ordered'),
                       data_type=Decimal),
                Column('cost', title=_(u'Cost'), data_type=currency,
                       format_func=get_formatted_cost)]

    def _update_widgets(self):
        if not self.quoted_items:
            has_selected = False
        else:
            has_selected = any([q.selected for q in self.quoted_items])
        self.create_order_button.set_sensitive(has_selected)

    def _can_purchase(self, purchaseitem):
        return (purchaseitem.cost > currency(0) and
                purchaseitem.quantity > Decimal(0))

    def _select_quotes(self, value):
        for item in self.quoted_items:
            item.selected = bool(value)
        self.quoted_items.refresh()
        self._update_widgets()

    def _cancel_group(self):
        msg = _("This will cancel the group and related quotes. "
                "Are you sure?")
        if not yesno(msg, gtk.RESPONSE_NO,
                     _("Cancel group"), _("Don't Cancel")):
            return

        store = api.new_store()
        group = store.fetch(self._group)
        group.cancel()
        store.remove(group)
        store.confirm(True)
        store.close()
        self.wizard.finish()

    def _get_purchase_from_quote(self, quote, store):
        quote_purchase = quote.purchase
        real_order = quote_purchase.clone()
        has_selected_items = False
        # add selected items
        for quoted_item in self.quoted_items:
            order = store.fetch(quoted_item.order)
            if order is quote_purchase and quoted_item.selected:
                purchase_item = store.fetch(quoted_item.item).clone()
                purchase_item.order = real_order
                has_selected_items = True

        # override some cloned data
        real_order.group = PaymentGroup(store=store)
        real_order.open_date = localtoday().date()
        real_order.quote_deadline = None
        real_order.status = PurchaseOrder.ORDER_PENDING

        if has_selected_items:
            return real_order
        else:
            store.remove(real_order)

    def _close_quotes(self, quotes):
        if not quotes:
            return

        if not yesno(_('Should we close the quotes used to compose the '
                       'purchase order ?'),
                     gtk.RESPONSE_NO, _("Close quotes"), _("Don't close")):
            return

        store = api.new_store()
        for q in quotes:
            quotation = store.fetch(q)
            quotation.close()
            store.remove(quotation)

        group = store.fetch(self._group)
        if group.get_items().is_empty():
            store.remove(group)

        store.confirm(True)
        store.close()
        self.wizard.finish()

    def _create_orders(self):
        store = api.new_store()
        group = store.fetch(self._group)
        quotes = []
        for quote in group.get_items():
            purchase = self._get_purchase_from_quote(quote, store)
            if not purchase:
                continue

            retval = run_dialog(PurchaseWizard, self.wizard, store, purchase)
            store.confirm(retval)
            # keep track of the quotes that might be closed
            if retval:
                quotes.append(quote)

        store.close()
        self._close_quotes(quotes)
    #
    # WizardStep
    #

    def post_init(self):
        self.wizard.enable_finish()
        self.wizard.next_button.set_label(gtk.STOCK_CLOSE)

    def has_next_step(self):
        return False

    #
    # Callbacks
    #

    def _on_quoted_items__selection_changed(self, widget, item):
        self._update_widgets()

    def on_select_all_button__clicked(self, widget):
        self._select_quotes(True)

    def on_unselect_all_button__clicked(self, widget):
        self._select_quotes(False)

    def on_cancel_group_button__clicked(self, widget):
        self._cancel_group()

    def on_create_order_button__clicked(self, widget):
        self._create_orders()


#
# Main wizards
#


class QuotePurchaseWizard(BaseWizard):
    size = (775, 400)

    def __init__(self, store, model=None):
        title = self._get_title(model)
        self.edit = model is not None
        self.quote = None
        self.quote_group = self._get_or_create_quote_group(model, store)
        model = model or self._create_model(store)
        if model.status != PurchaseOrder.ORDER_QUOTING:
            raise ValueError('Invalid order status. It should '
                             'be ORDER_QUOTING')

        first_step = StartQuoteStep(self, None, store, model)
        BaseWizard.__init__(self, store, first_step, model, title=title)

    def _get_title(self, model=None):
        if not model:
            return _('New Quote')
        return _('Edit Quote')

    def _create_model(self, store):
        supplier_id = sysparam.get_object_id('SUGGESTED_SUPPLIER')
        branch = api.get_current_branch(store)
        status = PurchaseOrder.ORDER_QUOTING
        group = PaymentGroup(store=store)
        return PurchaseOrder(supplier_id=supplier_id,
                             branch=branch, status=status,
                             expected_receival_date=None,
                             responsible=api.get_current_user(store),
                             group=group,
                             store=store)

    def _get_or_create_quote_group(self, order, store):
        if order is not None:
            quotation = store.find(Quotation, purchase=order).one()
            return quotation.group
        else:
            return QuoteGroup(branch=api.get_current_branch(store),
                              store=store)

    def _delete_model(self):
        if self.edit:
            return

        for item in self.model.get_items():
            self.store.remove(item)

        self.store.remove(self.model)

    #
    # WizardStep hooks
    #

    def finish(self):
        self._delete_model()
        self.retval = self.quote
        self.close()


class ReceiveQuoteWizard(BaseWizard):
    title = _("Receive Quote Wizard")
    size = (750, 450)

    def __init__(self, store):
        self.model = None
        first_step = QuoteGroupSelectionStep(self, store)
        BaseWizard.__init__(self, store, first_step, self.model)
        self.next_button.set_sensitive(False)

    #
    # WizardStep hooks
    #

    def finish(self):
        self.retval = self.model
        self.close()
