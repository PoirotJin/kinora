'use strict';
(() => {
  const dialog = document.getElementById('line-dialog');
  const frame = document.getElementById('line-frame');
  document.getElementById('line-open').onclick = () => {
    frame.src = 'line-player.html';
    dialog.showModal();
  };
  document.getElementById('line-close')?.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => frame.removeAttribute('src'));
})();
