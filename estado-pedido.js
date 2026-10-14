(() => {
  const {api,money,dateTime,query,escapeHtml,statusLabel,setMessage}=BYSG;
  const token=query('token'),message=document.getElementById('message');
  async function load(){
    try{
      const order=await api(`/api/orders/${encodeURIComponent(token)}`);
      document.querySelectorAll('a[href="seguimiento.html"]').forEach(link=>link.href=`seguimiento.html?token=${encodeURIComponent(token)}`);
      document.querySelectorAll('[data-order-number]').forEach(node=>node.textContent=order.order_number);
      document.querySelectorAll('[data-order-total]').forEach(node=>node.textContent=money(order.total_cents));
      document.querySelectorAll('[data-order-status]').forEach(node=>node.textContent=statusLabel(order.order_status));
      const detail=document.getElementById('orderDetail');
      if(detail)detail.innerHTML=`<div class="items">${order.items.map(item=>`<article class="item"><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.sku)} · ${escapeHtml(item.size_label)} · Cantidad ${item.quantity}</small></div><span class="money">${money(item.line_total_cents)}</span></article>`).join('')}</div><div class="totals"><div class="total-row"><span>Entrega</span><strong>${escapeHtml(order.delivery_service||statusLabel(order.delivery_method))}</strong></div><div class="total-row grand"><span>Total</span><strong>${money(order.total_cents)}</strong></div></div>`;
      const timeline=document.getElementById('timeline');
      if(timeline)timeline.innerHTML=order.events.map(event=>`<article><strong>${statusLabel(event.event_type)}</strong><div>${escapeHtml(event.detail)}</div><small>${dateTime(event.created_at)}</small></article>`).join('');
      const tracking=document.getElementById('tracking');
      if(tracking&&order.tracking_code){tracking.classList.remove('hidden');tracking.innerHTML=`Seguimiento: <strong>${escapeHtml(order.tracking_provider||'Correo')} · ${escapeHtml(order.tracking_code)}</strong>${order.tracking_url?` · <a href="${escapeHtml(order.tracking_url)}" rel="noreferrer">Ver envío</a>`:''}`}
    }catch(error){setMessage(message,error.message)}
  }
  load();
})();
