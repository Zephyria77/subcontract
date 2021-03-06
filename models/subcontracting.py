# -*- coding: utf-8 -*-
from datetime import datetime
from odoo.exceptions import UserError, AccessError, ValidationError

from odoo import models, api, fields, _


class SubcontractingWorkCenter(models.Model):
    _inherit = 'mrp.routing.workcenter'

    is_subcontract = fields.Boolean('Subcontracting')
    subcontract_vendor = fields.Many2one('res.partner', 'Supplier')
    subcontract_product = fields.Many2one('product.template', 'Product')
    subcontract_service_cost = fields.Integer('Cost Per Unit')
    subcontract_location = fields.Many2one('stock.location', 'Supplier Location')

    # Change Supplier Location based on Supplier.
    @api.multi
    @api.onchange('subcontract_vendor')
    def onchange_supplier_location(self):
        if self.is_subcontract:
            if self.subcontract_vendor.subcontracted_location:
                self.subcontract_location = self.subcontract_vendor.subcontracted_location.id
            else:
                self.subcontract_location = ''
                raise ValidationError(_("Please mention 'Subcontracted Location' for the selected Supplier."))


class VendorLocation(models.Model):
    _inherit = 'res.partner'

    subcontracted_location = fields.Many2one('stock.location', 'Subcontracted Location')


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    manufacturing_order_number = fields.Many2one('mrp.production', "Manufacturing Order", store=True)
    sale_order_number = fields.Many2one('sale.order', "Sale Order", store=True)


class MRP(models.Model):
    _inherit = 'mrp.production'

    @api.multi
    def button_plan(self):
        """ Create work orders. And probably do stuff, like things. """
        orders_to_plan = self.filtered(lambda order: order.routing_id and order.state == 'confirmed')
        for order in orders_to_plan:
            quantity = order.product_uom_id._compute_quantity(order.product_qty,
                                                              order.bom_id.product_uom_id) / order.bom_id.product_qty
            boms, lines = order.bom_id.explode(order.product_id, quantity, picking_type=order.bom_id.picking_type_id)
            order._generate_workorders(boms)

            # Get Subcontracting info from work operations and update it in related work order.
        for operations in self.routing_id.operation_ids:
            for workorder in self.workorder_ids:
                if operations.name == workorder.name:
                    if operations.is_subcontract is True:
                        workorder.update({
                            'is_subcontract': True,
                            'subcontract_wo_vendor': operations.subcontract_vendor.id,
                            'subcontract_wo_product': operations.subcontract_product.id,
                            'subcontract_wo_service_cost': operations.subcontract_service_cost,
                            'subcontract_supplier_location': operations.subcontract_location.id,
                        })

        # Save Previous Work Order Record.
        for workorder in self.workorder_ids:
            first_workorder = min(self.workorder_ids)
            if first_workorder.id == workorder.id:
                if workorder.next_work_order_id.id:
                    workorder.update({
                        'previous_workorder_id': None
                    })
                    continue
            else:
                work_id = workorder.id - 1
                search_work_id = self.env['mrp.workorder'].search([('id', '=', work_id)])
                workorder.update({
                    'previous_workorder_id': search_work_id.id
                })
        return orders_to_plan.write({'state': 'planned'})


class SubcontractingWorkOrder(models.Model):
    _inherit = 'mrp.workorder'

    is_subcontract = fields.Boolean('Subcontracting', store=True, default=False)
    subcontract_wo_vendor = fields.Many2one('res.partner', 'Supplier', store=True)
    subcontract_wo_product = fields.Many2one('product.template', 'Product', store=True)
    subcontract_wo_service_cost = fields.Float('Cost Per Unit', store=True)
    subcontract_supplier_location = fields.Many2one('stock.location', "Supplier Location", store=True)

    is_rfq = fields.Boolean('RFQ', default=False)

    rfq_ids = fields.Many2one('purchase.order', 'RFQ')

    previous_workorder_id = fields.Many2one('mrp.workorder', readonly=True, store=True)

    delivery_challan_id = fields.Many2one('stock.picking', readonly=True, store=True)
    delivery_challan = fields.Boolean(readonly=True, store=True, default=False)

    # Change Supplier Location based on Supplier.
    @api.multi
    @api.onchange('subcontract_wo_vendor')
    def onchange_supplier_location(self):
        if self.is_subcontract:
            if self.subcontract_wo_vendor.subcontracted_location:
                self.subcontract_supplier_location = self.subcontract_wo_vendor.subcontracted_location.id
            else:
                self.subcontract_supplier_location = ''
                raise ValidationError(_("Please mention 'Subcontracted Location' for the selected Supplier."))

    def po(self):
        purchase_id = self.env.ref('purchase.purchase_order_form').id
        search_po = self.env['purchase.order'].search([('origin', '=', self.name)], limit=1).id
        return {
            'type': 'ir.actions.act_window',
            'view_type': 'form',
            'view_mode': 'form',
            'res_model': 'purchase.order',
            'views': [(purchase_id, 'form')],
            'view_id': purchase_id,
            'res_id': search_po,
            'target': 'current',
        }

    # Create RFQ for work order and product inside the work order.
    def create_rfq(self):
        receipt = self.env['stock.picking.type'].search([('name', '=', 'Receipts')], limit=1)
        product = self.env['product.product'].search([('name', '=', self.subcontract_wo_product.name)])
        sale_id = self.env['sale.order'].search([('name', '=', self.production_id.origin)])

        for orders in self:
            rfq = self.env['purchase.order'].create({
                'partner_id': orders.subcontract_wo_vendor.id,
                'date_order': datetime.now(),
                'origin': self.name,
                'date_planned': datetime.now(),
                'picking_type_id': receipt.id,
                'manufacturing_order_number': self.production_id.id,
                'sale_order_number': sale_id.id if sale_id.id else None
            })

            order_line = self.env['purchase.order.line'].create({
                'order_id': rfq.id,
                'product_id': product.id,
                'name': product.name,
                'date_planned': datetime.now(),
                'product_qty': 1,
                'product_uom': product.uom_po_id.id,
                'price_unit': orders.subcontract_wo_service_cost,
            })

            self.update({
                'rfq_ids': rfq.id
            })

        self.update({'is_rfq': True})

    @api.multi
    def button_start(self):
        mrp = self.env['mrp.production'].search([('name', '=', self.production_id.name)])
        for workorder in mrp.workorder_ids:
            if workorder.state == 'progress':
                raise ValidationError(
                    _("Please finish the 'In Progress' Work Order first to proceed with the next one."))
            else:
                pass
        res = super(SubcontractingWorkOrder, self).button_start()

        # Internal Stock Move if No Previous WorkOrder ID found.
        # 1st Condition : If 1st workorder then Source Location will be from MO and Destination Location will be from WorkOrder Supplier Location.
        for records in mrp:
            if self.next_work_order_id:
                if not self.previous_workorder_id:
                    move_location = self.env['wiz.stock.move.location'].create({
                        'origin_location_id': records.location_src_id.id,
                        'destination_location_id': self.subcontract_supplier_location.id
                    })
                    for recs in records.move_raw_ids:
                        if recs.reserved_availability == recs.product_uom_qty:
                            product = self.env['product.product'].search([('name', '=', recs.product_id.name)])
                            move_location_line = self.env['wiz.stock.move.location.line'].create({
                                'product_id': product.id,
                                'product_uom_id': recs.product_uom.id,
                                'origin_location_id': records.location_src_id.id,
                                'destination_location_id': self.subcontract_supplier_location.id,
                                'move_quantity': float(recs.reserved_availability) if float(
                                    recs.reserved_availability) else 0.0,
                                'max_quantity': float(recs.reserved_availability) if float(
                                    recs.reserved_availability) else 0.0,
                                'move_location_wizard_id': move_location.id
                            })
                        else:
                            raise ValidationError(_(
                                "All products don't have reserved qty available. Please reserve the qty which are needed to be consume "
                                "before starting the work order."))
                    move_location.action_move_location()
                    self.update({
                        'delivery_challan_id': move_location.picking_id.id,
                        'delivery_challan': True
                    })

        # Internal Stock Move if Previous WorkOrder ID found.
        # 2nd Condition : If Source Location and Destination Location is different then Source Location will be from Previous WO and Destination Location
        #                 will be from current WO.
        for records in mrp:
            if self.next_work_order_id:
                if self.previous_workorder_id and self.previous_workorder_id.state == 'done':
                    if self.previous_workorder_id.subcontract_supplier_location.id != self.subcontract_supplier_location.id:
                        move_location = self.env['wiz.stock.move.location'].create({
                            'origin_location_id': self.previous_workorder_id.subcontract_supplier_location.id,
                            'destination_location_id': self.subcontract_supplier_location.id
                        })
                        for recs in records.move_raw_ids:
                            product = self.env['product.product'].search([('name', '=', recs.product_id.name)])
                            move_location_line = self.env['wiz.stock.move.location.line'].create({
                                'product_id': product.id,
                                'product_uom_id': recs.product_uom.id,
                                'origin_location_id': self.previous_workorder_id.subcontract_supplier_location.id,
                                'destination_location_id': self.subcontract_supplier_location.id,
                                'move_quantity': float(recs.reserved_availability) if float(
                                    recs.reserved_availability) else recs.product_uom_qty,
                                'max_quantity': float(recs.reserved_availability) if float(
                                    recs.reserved_availability) else recs.product_uom_qty,
                                'move_location_wizard_id': move_location.id
                            })
                        move_location.action_move_location()
                        self.update({
                            'delivery_challan_id': move_location.picking_id.id,
                            'delivery_challan': True
                        })

        # Internal Stock Move if last WorkOrder.
        # 3rd Condition : - If Next WorkOrder is not available Source Location will be Picked up from the Supplier Location of Last WO and Destination
        #                   will be Picked up from Raw material Location of MO.
        for records in mrp:
            if not self.next_work_order_id:
                if self.previous_workorder_id and self.previous_workorder_id.state == 'done':
                    move_location = self.env['wiz.stock.move.location'].create({
                        'origin_location_id': self.previous_workorder_id.subcontract_supplier_location.id,
                        'destination_location_id': self.subcontract_supplier_location.id
                    })
                    for recs in records.move_raw_ids:
                        product = self.env['product.product'].search([('name', '=', recs.product_id.name)])
                        move_location_line = self.env['wiz.stock.move.location.line'].create({
                            'product_id': product.id,
                            'product_uom_id': recs.product_uom.id,
                            'origin_location_id': self.previous_workorder_id.subcontract_supplier_location.id,
                            'destination_location_id': self.subcontract_supplier_location.id,
                            'move_quantity': float(recs.reserved_availability) if float(
                                recs.reserved_availability) else recs.product_uom_qty,
                            'max_quantity': float(recs.reserved_availability) if float(
                                recs.reserved_availability) else recs.product_uom_qty,
                            'move_location_wizard_id': move_location.id
                        })
                    move_location.action_move_location()
                if self.previous_workorder_id and self.previous_workorder_id.state == 'done':
                    move_location = self.env['wiz.stock.move.location'].create({
                        'origin_location_id': self.subcontract_supplier_location.id,
                        'destination_location_id': records.location_src_id.id
                    })
                    for recs in records.move_raw_ids:
                        product = self.env['product.product'].search([('name', '=', recs.product_id.name)])
                        move_location_line = self.env['wiz.stock.move.location.line'].create({
                            'product_id': product.id,
                            'product_uom_id': recs.product_uom.id,
                            'origin_location_id': self.subcontract_supplier_location.id,
                            'destination_location_id': records.location_src_id.id,
                            'move_quantity': float(recs.reserved_availability) if float(
                                recs.reserved_availability) else recs.product_uom_qty,
                            'max_quantity': float(recs.reserved_availability) if float(
                                recs.reserved_availability) else recs.product_uom_qty,
                            'move_location_wizard_id': move_location.id
                        })
                    move_location.action_move_location()
                    self.update({
                        'delivery_challan_id': move_location.picking_id.id,
                        'delivery_challan': True
                    })

        return res

    def print_delivery_challan(self):
        picking = self.delivery_challan_id
        source_loc = ''
        dest_loc = ''
        if picking.location_id.partner_id.id:
            street = str(picking.location_id.partner_id.street)
            street2 = str(picking.location_id.partner_id.street2)
            city = str(picking.location_id.partner_id.city)
            state = str(picking.location_id.partner_id.state_id.name)
            zip = str(picking.location_id.partner_id.zip)
            country = str(picking.location_id.partner_id.country_id.name)
            source_loc = street + ',' + street2 + ',' + city + ',' + state + ',' + zip + ',' + country + '.'

        if picking.location_dest_id.partner_id.id:
            street = str(picking.location_dest_id.partner_id.street)
            street2 = str(picking.location_dest_id.partner_id.street2)
            city = str(picking.location_dest_id.partner_id.city)
            state = str(picking.location_dest_id.partner_id.state_id.name)
            zip = str(picking.location_dest_id.partner_id.zip)
            country = str(picking.location_dest_id.partner_id.country_id.name)
            dest_loc = street + ',' + street2 + ',' + city + ',' + state + ',' + zip + ',' + country + '.'

        product_values = []

        values = [{
            'picking_id': picking.name,
            'start_date': picking.date,
            'end_date': picking.date_done,
            'source_location': source_loc if source_loc else picking.location_id.display_name,
            'destination_location': dest_loc if dest_loc else picking.location_dest_id.display_name,
            'next_workorder_id': self.next_work_order_id.id if self.next_work_order_id.id else False,
            'finished_product': self.product_id.name,
            'qty_finished_product': self.qty_produced
        }]
        for record in picking.move_lines:
            product_vals = {
                'product_id': record.product_id.name,
                'qty': record.product_qty,
                'uom': record.product_uom.name,
            }
            product_values.append(product_vals)

        work_id = [{
            'next_workorder': self.next_work_order_id.id if self.next_work_order_id.id else False,

        }]

        return self.env.ref('subcontract.action_report_delivery').report_action(self, data={'values': values,
                                                                                            'product_values': product_values,
                                                                                            'work_id': work_id})
