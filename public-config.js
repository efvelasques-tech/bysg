(() => {
  const formatCuit=value=>/^\d{11}$/.test(value)?`${value.slice(0,2)}-${value.slice(2,10)}-${value.slice(10)}`:value;
  fetch('/api/public/config',{credentials:'same-origin'})
    .then(response=>response.ok?response.json():Promise.reject(new Error('Configuración no disponible')))
    .then(config=>{
      document.querySelectorAll('[data-public-text]').forEach(element=>{
        const key=element.dataset.publicText;
        if(config[key])element.textContent=key==='legal_cuit'?formatCuit(config[key]):config[key];
      });
      document.querySelectorAll('[data-public-email]').forEach(link=>{
        if(!config.contact_email)return;
        link.textContent=config.contact_email;
        link.href=`mailto:${config.contact_email}`;
      });
    })
    .catch(()=>{});
})();
