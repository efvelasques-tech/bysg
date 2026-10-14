window.BYSG = (() => {
  const money = cents => new Intl.NumberFormat('es-AR',{style:'currency',currency:'ARS',maximumFractionDigits:0}).format((Number(cents)||0)/100);
  const dateTime = value => value ? new Intl.DateTimeFormat('es-AR',{dateStyle:'short',timeStyle:'short'}).format(new Date(value)) : '—';
  const escapeHtml = value => String(value??'').replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const labels = {
    pending:'Pendiente',approved:'Aprobado',rejected:'Rechazado',refunded:'Devuelto',
    payment_pending:'Esperando pago',paid:'Pagado',payment_rejected:'Pago rechazado',
    cancelled:'Cancelado',in_production:'En producción',ready_for_pickup:'Listo para retirar',
    shipped:'Enviado',delivered:'Entregado',pickup:'Retiro coordinado',local:'Entrega a domicilio',mail:'Correo con seguimiento',home_delivery:'Entrega a domicilio',mercado_envios:'Mercado Envíos',correo_argentino:'Correo Argentino',courier:'Courier a coordinar',international_key_west:'Envío internacional a Key West'
  };
  const statusLabel = value => labels[value]||String(value||'—').replaceAll('_',' ');
  async function api(path,options={}){
    const config={credentials:'same-origin',...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}};
    const response=await fetch(path,config);
    let payload={};
    try{payload=await response.json()}catch(error){}
    if(!response.ok)throw new Error(payload.error||`Error ${response.status}`);
    return payload;
  }
  const query = name => new URLSearchParams(location.search).get(name)||'';
  const setMessage = (element,message,type='danger') => {
    element.textContent=message;
    element.className=`notice ${type}`;
    element.classList.toggle('hidden',!message);
  };
  return {money,dateTime,escapeHtml,statusLabel,api,query,setMessage};
})();
