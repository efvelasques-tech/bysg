(() => {
  const checkoutButton=document.getElementById('cartCheckout');
  const warning=document.querySelector('.cart-warning');
  let backendReady=false;
  let catalog=null,health=null;
  const realCheckout=()=>health?.payment_mode==='mercado_pago'&&!health.sandbox;
  const cartHasUnapprovedPrice=()=>{
    if(!catalog||!window.BYSGStore)return true;
    const typeCode={Remeras:'REM',Buzos:'BUZ',Gorras:'GOR'};
    return window.BYSGStore.getCart().some(line=>{
      const product=catalog.products.find(item=>item.category_code===typeCode[line.productType]&&Number(item.design_number)===Number(line.page));
      return !product||Boolean(product.is_sample_price)||Number(product.price_cents)<=0;
    });
  };
  const updateCheckoutState=()=>{
    if(!checkoutButton||!window.BYSGStore)return;
    const units=window.BYSGStore.getCart().reduce((sum,line)=>sum+line.qty,0);
    const realUnavailable=realCheckout()&&!health.real_payments_enabled;
    const unapproved=realCheckout()&&units>0&&cartHasUnapprovedPrice();
    checkoutButton.disabled=!backendReady||units<1||realUnavailable||unapproved;
    if(warning&&backendReady){
      warning.textContent=realUnavailable?'Los cobros reales todavía no están habilitados. La tienda permanece en prueba.':
        unapproved?'Este carrito tiene precios de muestra. No se puede iniciar un cobro real hasta aprobarlos.':
        health?.payment_mode==='mock'||health?.sandbox?'Pago de prueba activo: ningún cobro real será realizado.':
        catalog?.price_mode==='sample'?'Hay productos con precios de muestra; solo podés comprar los que ya tienen precio final.':
        'El pago se procesa en el entorno seguro de Mercado Pago.';
    }
  };
  async function connect(){
    if(!window.BYSGStore)return;
    try{
      const [catalogResponse,healthResponse,configResponse]=await Promise.all([
        fetch('/api/catalog',{credentials:'same-origin'}),
        fetch('/api/health',{credentials:'same-origin'}),
        fetch('/api/public/config',{credentials:'same-origin'})
      ]);
      if(!catalogResponse.ok||!healthResponse.ok)throw new Error('Servidor no disponible');
      const data=await catalogResponse.json(),healthData=await healthResponse.json(),publicConfig=configResponse.ok?await configResponse.json():{};
      catalog=data;
      health=healthData;
      backendReady=true;
      window.BYSGStore.setGlobalStock(data.stock||[]);
      window.BYSGStore.setCatalogData(data);
      document.querySelectorAll('[data-social]').forEach(link=>{const value=publicConfig[link.dataset.social];if(value){link.href=value;link.target='_blank';link.rel='noreferrer'}});
      if(checkoutButton){
        checkoutButton.innerHTML=healthData.payment_mode==='mock'?'<span class="mp-mark">MP</span>Continuar al pago de prueba':'<span class="mp-mark">MP</span>Continuar a Mercado Pago';
        checkoutButton.addEventListener('click',()=>{
          if(checkoutButton.disabled||!window.BYSGStore.getCart().length)return;
          location.href=new URL('checkout.html',location.href).href;
        });
      }
      updateCheckoutState();
    }catch(error){
      backendReady=false;
      if(warning)warning.textContent='La tienda está en modo visual. Iniciá el servidor BYSG para probar pedidos y pagos.';
      updateCheckoutState();
    }
  }
  const cartCount=document.getElementById('cartCount');
  if(cartCount)new MutationObserver(updateCheckoutState).observe(cartCount,{childList:true,characterData:true,subtree:true});
  connect();
})();
