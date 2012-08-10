# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2012 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s): Stoq Team <stoq-devel@async.com.br>
##


from stoqlib.gui.uitestutils import GUITest
from stoqlib.gui.editors.producteditor import ProductEditor


class TestProductEditor(GUITest):
    def testCreate(self):
        editor = ProductEditor(self.trans)
        self.check_editor(editor, 'editor-product-create',
                          ignores=[str(editor.model.sellable.id)])

    def testShow(self):
        product = self.create_product()
        editor = ProductEditor(self.trans, product)
        self.check_editor(editor, 'editor-product-show',
                          ignores=[str(editor.model.sellable.id)])