// Defense in depth: this static preview cannot submit forms or start checkout.
document.addEventListener('submit', event => {
  event.preventDefault();
  event.stopImmediatePropagation();
  window.alert('Esta vista es solo de prueba. No se aceptan datos, pedidos ni pagos.');
}, true);
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form').forEach(form => {
    form.querySelectorAll('input,select,textarea,button').forEach(field => { field.disabled = true; });
  });
  const checkout = document.getElementById('cartCheckout');
  if (checkout) checkout.disabled = true;
});
