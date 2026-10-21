/* Cotejo en vivo (lab/cotejo.html). Sin dependencias.
   - /data/cotejo/ejemplos.json (lo escribe demo/exportar_ejemplos.py en el repositorio de Cotejo): tres escritos con el
     informe que dio Cotejo al exportarlos, con la fecha y el commit. Se ven al instante y no gastan consultas.
   - «Verificar en vivo»: POST {texto} a data-api + "verificar" (la Function URL de la demo; en localhost, ?api= la
     cambia). La forma de la respuesta y los códigos de error son los de demo/DISEÑO.md §2.
   - El informe se dibuja desde el JSON de informe.a_json con las reglas de informe.a_html: mismo orden, mismos grupos,
     mismo resaltado de diferencias, para que la página y el informe impreso no se contradigan.
   Todo lo que viene del JSON entra con textContent, nunca como HTML: el escrito lo pega el visitante y los pasajes son
   texto de la Corte, y cualquiera de los dos puede traer un «<». */
(function () {
  'use strict';

  var L = window.LAB || { lang: function () { return 'es'; }, onLang: function () {} };
  var MAX = 5000;              // MAX_CARACTERES de la Lambda
  // verificar() corta a los 60 s y la Lambda a los 180 s: si a los 150 s no hay respuesta, algo va mal y es mejor decirlo
  var ESPERA_MS = 150000;
  var ORDEN = ['no_existe', 'no_encontrada', 'con_observaciones', 'sin_confirmar', 'verificada', 'no_verificable'];
  var ETQ = {
    verificada: 'Verificada|Verified', con_observaciones: 'Con observaciones|With observations',
    no_existe: 'No existe|Does not exist', no_encontrada: 'No encontrada|Not found',
    sin_confirmar: 'Sin confirmar|Unconfirmed', no_verificable: 'No verificable|Not verifiable'
  };
  // [singular, plural] para el resumen corto de cada ejemplo («5 no existen»)
  var PL = {
    verificada: [['verificada', 'verificadas'], ['verified', 'verified']],
    con_observaciones: [['con observaciones', 'con observaciones'], ['with observations', 'with observations']],
    no_existe: [['no existe', 'no existen'], ['does not exist', 'do not exist']],
    no_encontrada: [['no encontrada', 'no encontradas'], ['not found', 'not found']],
    sin_confirmar: [['sin confirmar', 'sin confirmar'], ['unconfirmed', 'unconfirmed']],
    no_verificable: [['no verificable', 'no verificables'], ['not verifiable', 'not verifiable']]
  };
  // Los grupos de informe.a_html: primero lo que puede costarle una sanción al abogado, al final lo que no se miró
  var GRUPOS = [
    ['Requieren atención|Need attention', ['no_existe', 'no_encontrada', 'con_observaciones', 'sin_confirmar']],
    ['Verificadas|Verified', ['verificada']],
    ['No verificadas en esta versión|Not checked in this version', ['no_verificable']]
  ];
  var TRIBUNAL = {
    'Corte Constitucional': 'Corte Constitucional|Constitutional Court',
    'Corte Suprema de Justicia': 'Corte Suprema de Justicia|Supreme Court of Justice',
    'Consejo de Estado': 'Consejo de Estado|Council of State'
  };
  // Sala del buscador de la Corte Suprema por prefijo (cotejo/fuentes/corte_suprema.py, SALAS). Hace falta para
  // enlazar la búsqueda ya hecha cuando una providencia no aparece: es lo que le permite al abogado comprobarla.
  var SALAS = {
    SC: 'Civil', AC: 'Civil', SL: 'Laboral', AL: 'Laboral', SP: 'Penal', AP: 'Penal', AHP: 'Penal',
    STC: 'Tutelas', ATC: 'Tutelas', STL: 'Tutelas', ATL: 'Tutelas', STP: 'Tutelas', ATP: 'Tutelas'
  };
  var BUSCADOR_CSJ = 'https://consultaprovidencias.cortesuprema.gov.co/';
  var CODIGOS = ['mal_formada', 'texto_vacio', 'texto_largo', 'demasiadas_citas', 'limite_ip', 'limite_global', 'interno', 'ruta'];
  // MAX_CITAS y MAX_PROVIDENCIAS de la Lambda (DISEÑO.md §2). Van fijos, como el 5.000: los repiten los límites de la
  // demo en cotejo.html, y si la Lambda los cambia hay que cambiar los dos sitios.
  var MAX_CITAS = 40, MAX_PROV = 15;

  var API = '';
  var D = null, EJ = {};
  /* sel: qué texto está elegido (id de un ejemplo o 'propio'); textos: lo que hay en el área para cada selección, el
     borrador propio y cada ejemplo editado por separado (editar un ejemplo convertía el texto en «Tu texto» y pisaba,
     sin preguntar, el borrador del visitante y el informe en vivo que le había costado una consulta); res: informes en
     vivo por clave de selección; estado: el aviso bajo el botón; mostrado y viejo: qué informe está dibujado y si es de
     otro texto, para no redibujarlo en cada tecla; cerrado: el día de Colombia en que el servidor respondió
     limite_global (ese día ya no atiende a nadie, y la línea del cupo no puede seguir diciendo «te quedan 2»). */
  var S = { sel: 'propio', textos: { propio: '' }, res: {}, estado: null, cargando: false, t0: 0, restantes: null, abajo: false,
    mostrado: null, viejo: null, cerrado: null };
  var tocado = false, reloj = 0;

  /* ------------------------------------------------------------ utilidades */
  function lg(l) { return l || L.lang(); }
  function en(l) { return lg(l) === 'en'; }
  function tt(es, x) { return en() ? x : es; }
  function lt(s) { var p = String(s).split('|'); return p.length === 2 ? p[en() ? 1 : 0] : s; }
  function nm(o) { return o == null ? '' : (o[L.lang()] != null ? o[L.lang()] : o.es); }
  function $(id) { return document.getElementById(id); }
  function h(tag, at, kids) {
    var e = document.createElement(tag);
    if (at) for (var k in at) {
      var v = at[k];
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v; else e.setAttribute(k, v === true ? '' : String(v));
    }
    add(e, kids);
    return e;
  }
  function add(e, kids) {
    if (kids == null) return;
    if (!Array.isArray(kids)) kids = [kids];
    kids.forEach(function (c) {
      if (c == null || c === false || c === '') return;
      if (Array.isArray(c)) { add(e, c); return; }
      e.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
    });
  }
  // [a, ' · ', b, ' · ', c]: une piezas (texto o nodos) con un separador
  function unir(xs, sep) {
    var out = [];
    xs.forEach(function (x) { if (x == null || x === '') return; if (out.length) out.push(sep); out.push(x); });
    return out;
  }
  var NF = {};
  function num(x, d, l) {
    var k = lg(l) + (d || 0);
    // es-CO agrupa los millares también con cuatro cifras («5.000»), como el resto del sitio
    if (!NF[k]) NF[k] = new Intl.NumberFormat(en(l) ? 'en-US' : 'es-CO', { maximumFractionDigits: d || 0 });
    return NF[k].format(x);
  }
  function pct(x) { return num(x, 1) + (en() ? '%' : ' %'); }
  function fecha(iso, l) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    if (!m) return iso || '';
    try {
      // En UTC: la fecha de una providencia no tiene hora, y en la zona del visitante podría salir el día anterior
      return new Intl.DateTimeFormat(en(l) ? 'en-US' : 'es-CO', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })
        .format(new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])));
    } catch (e) { return iso; }
  }
  function hora(d) {
    try { return new Intl.DateTimeFormat(en() ? 'en-US' : 'es-CO', { hour: '2-digit', minute: '2-digit' }).format(d); }
    catch (e) { return ''; }
  }
  /* Caracteres como los cuenta Python (len, puntos de código), que es quien aplica el límite de 5.000: un emoji son dos
     unidades de JavaScript y uno solo para la Lambda. Contarlo distinto bloquearía textos que el servidor acepta. */
  function largo(s) {
    var n = 0;
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (c >= 0xD800 && c <= 0xDBFF && i + 1 < s.length) { var d = s.charCodeAt(i + 1); if (d >= 0xDC00 && d <= 0xDFFF) i++; }
      n++;
    }
    return n;
  }
  // informe._plano: una sola línea, sin los saltos que trae el texto de la relatoría
  function plano(s) { return String(s || '').split(/\s+/).filter(Boolean).join(' '); }
  function enlace(url, txt) {
    // Las URL vienen del JSON: solo se enlaza https, para que ningún dato pueda convertirse en un javascript:
    if (!/^https:\/\//i.test(url || '')) return txt;
    return h('a', { href: url, target: '_blank', rel: 'noopener' }, txt);
  }
  function estadoValido(e) { return ETQ[e] ? e : 'sin_confirmar'; }

  /* ------------------------------------------------------------ API y cupo */
  function resolverApi() {
    var m = $('main'), a = ((m && m.getAttribute('data-api')) || '').trim();
    // ?api= solo en local: en producción, un enlace con ?api= mandaría el texto del visitante a un servidor ajeno
    if (/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) {
      var q = null;
      try { q = new URLSearchParams(location.search).get('api'); } catch (e) {}
      if (q && /^https?:\/\/\S+$/i.test(q)) a = q;
    }
    if (a && !/\/$/.test(a)) a += '/';
    return a;
  }
  // El cupo es por día de Colombia (UTC-5, sin horario de verano), como lo cuenta cuota.py
  function hoyColombia() { return new Date(Date.now() - 5 * 3600000).toISOString().slice(0, 10); }
  function msHastaMedianoche() { return 86400000 - ((Date.now() - 5 * 3600000) % 86400000); }
  /* Lo que queda de cupo se recuerda en este navegador solo para mostrarlo al volver; quien decide es el servidor.
     Si el almacenamiento no está (ventana privada, bloqueado), la página dice el límite general y ya. */
  function leerCupo() {
    try {
      var j = JSON.parse(localStorage.getItem('cotejo-cupo') || 'null');
      if (j && j.d === hoyColombia() && typeof j.n === 'number') return j.n;
    } catch (e) {}
    return null;
  }
  function guardarCupo(n) { try { localStorage.setItem('cotejo-cupo', JSON.stringify({ d: hoyColombia(), n: n })); } catch (e) {} }

  /* ------------------------------------------------------------ selector de texto */
  function resumenCorto(r) {
    if (!r) return '';
    var i = en() ? 1 : 0, partes = [];
    ORDEN.forEach(function (e) { var n = r[e] || 0; if (n) partes.push(n + ' ' + PL[e][i][n === 1 ? 0 : 1]); });
    return partes.join(' · ');
  }

  function renderPick() {
    var g = $('ct-pick'), propio = g.querySelector('[data-ej="propio"]');
    var a = document.activeElement, foco = a && g.contains(a) ? a.getAttribute('data-ej') : null;
    [].slice.call(g.querySelectorAll('[data-ej]')).forEach(function (b) { if (b !== propio) g.removeChild(b); });
    if (D) D.ejemplos.forEach(function (e) {
      var inf = e.respuesta && e.respuesta.informe, k = inf ? resumenCorto(inf.resumen) : '';
      g.insertBefore(h('button', { type: 'button', class: 'ct-pk', role: 'radio', 'aria-checked': 'false', tabindex: '-1', 'data-ej': e.id }, [
        h('span', { class: 'n' }, nm(e.titulo)),
        h('span', { class: 'd' }, nm(e.descripcion)),
        k ? h('span', { class: 'k' }, tt('Informe guardado: ', 'Saved report: ') + k) : null
      ]), propio);
    });
    marcarPick();
    if (foco) { var n = g.querySelector('[data-ej="' + foco + '"]'); if (n) n.focus({ preventScroll: true }); }
  }
  function marcarPick() {
    [].slice.call($('ct-pick').querySelectorAll('[data-ej]')).forEach(function (b) {
      var on = b.getAttribute('data-ej') === S.sel;
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.setAttribute('tabindex', on ? '0' : '-1');
    });
  }
  function pickKeys(ev) {
    var k = ev.key;
    if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'].indexOf(k) < 0) return;
    var bs = [].slice.call($('ct-pick').querySelectorAll('[role="radio"]')), i = bs.indexOf(document.activeElement);
    if (i < 0) return;
    ev.preventDefault();
    var n = k === 'Home' ? 0 : k === 'End' ? bs.length - 1 : (i + (k === 'ArrowRight' || k === 'ArrowDown' ? 1 : -1) + bs.length) % bs.length;
    bs[n].focus();
    elegir(bs[n].getAttribute('data-ej'), false);
  }

  // Un objeto por ejemplo, siempre el mismo: refrescarInforme compara por identidad para no redibujar en cada tecla
  var GU = {};
  function guardado(id) {
    var e = EJ[id];
    if (!GU[id] && e && e.respuesta) GU[id] = { origen: 'guardado', respuesta: e.respuesta, texto: e.texto, ej: id, clave: id };
    return GU[id] || null;
  }
  function textoDe(id) { return S.textos[id] != null ? S.textos[id] : id === 'propio' ? '' : EJ[id].texto; }
  function editado() { return !!(EJ[S.sel] && $('ct-texto').value !== EJ[S.sel].texto); }
  /* Lo que se enseña para la selección actual: primero el informe que es del texto que hay en el área (el en vivo o el
     guardado del ejemplo); si ninguno lo es, el en vivo, y si no hay, el guardado, los dos con el aviso de texto cambiado */
  function actual() {
    var v = S.res[S.sel], g = guardado(S.sel), t = $('ct-texto').value;
    if (v && v.texto === t) return v;
    if (g && g.texto === t) return g;
    return v || g;
  }

  function elegir(id, foco) {
    if (id !== 'propio' && !EJ[id]) return;
    var ta = $('ct-texto');
    S.textos[S.sel] = ta.value;
    tocado = true;
    S.sel = id;
    ta.value = textoDe(id);
    if (!S.cargando) S.estado = null;
    marcarPick();
    contar();
    pintarEstado();
    renderInforme();
    escribirHash();
    var r = actual();
    anunciar(id === 'propio' ? tt('Tu texto.', 'Your text.') :
      nm(EJ[id].titulo) + (editado() ? tt(', editado', ', edited') : '') + '. ' +
      (r ? (r.origen === 'vivo' ? tt('Informe en vivo: ', 'Live report: ') : tt('Informe guardado: ', 'Saved report: ')) +
        resumenCorto(r.respuesta.informe.resumen) + '.' : ''));
    if (id === 'propio' && foco) ta.focus();
  }

  function alEscribir() {
    var ta = $('ct-texto');
    tocado = true;
    // Editar un ejemplo lo deja como ejemplo editado, en su propia ranura: su informe se queda a la vista con el aviso
    // de que ya no es del texto que hay en el área (así se puede comparar, pero nadie lo lee como el informe del texto
    // nuevo), y «Tu texto» conserva su borrador y su informe.
    S.textos[S.sel] = ta.value;
    contar();
    limpiarAvisoDeTexto();
    refrescarInforme();
  }
  function refrescarInforme() {
    var r = actual(), viejo = !!(r && r.texto !== $('ct-texto').value);
    if (r !== S.mostrado || viejo !== S.viejo) renderInforme();
  }
  /* Un «pasa de 5.000 caracteres» o un «no hay texto» dejan de ser verdad en cuanto se corrige el texto, y el contador
     y el botón ya lo dicen: si el aviso se quedara hasta el siguiente clic, la página se contradiría. Los avisos del
     servidor (demasiadas_citas, cupo, red) se quedan: hablan del texto que se envió, y así lo dicen. */
  function limpiarAvisoDeTexto() {
    var e = S.estado, t = $('ct-texto').value;
    if (S.cargando || !e) return;
    if ((e.codigo === 'texto_largo' && largo(t) <= MAX) || (e.codigo === 'texto_vacio' && t.trim())) {
      S.estado = null;
      pintarEstado();
    }
  }
  function restaurar() {
    if (!EJ[S.sel]) return;
    var ta = $('ct-texto');
    ta.value = EJ[S.sel].texto;
    S.textos[S.sel] = ta.value;
    contar();
    limpiarAvisoDeTexto();
    refrescarInforme();
    // El botón se oculta al pulsarlo: sin esto el foco caería en el body
    ta.focus();
    anunciar(tt('Texto original del ejemplo.', 'Original sample text.'));
  }

  /* ------------------------------------------------------------ contador, botón y avisos */
  function contar() {
    var ta = $('ct-texto'), n = largo(ta.value), c = $('ct-cuenta'), over = n > MAX;
    c.textContent = num(n) + ' / ' + num(MAX) + tt(' caracteres', ' characters') +
      (over ? tt(' · sobran ' + num(n - MAX), ' · ' + num(n - MAX) + ' too many') : '');
    c.classList.toggle('is-over', over);
    ta.classList.toggle('is-over', over);
    if (over) ta.setAttribute('aria-invalid', 'true'); else ta.removeAttribute('aria-invalid');
    // El ejemplo editado no tiene otra forma de volver a su texto: el área de texto pierde el deshacer al cambiar de ejemplo
    $('ct-editado').hidden = !editado();
    boton();
  }
  /* aria-disabled y no disabled: un botón deshabilitado pierde el foco, y quien lo pulsó con el teclado se queda sin
     saber dónde está mientras la verificación corre */
  function boton() {
    var b = $('ct-verificar'), no = S.cargando || !API || largo($('ct-texto').value) > MAX;
    b.setAttribute('aria-disabled', no ? 'true' : 'false');
    b.textContent = S.cargando ? tt('Verificando…', 'Checking…') : tt('Verificar en vivo', 'Check live');
  }

  function cupo() {
    var n = S.restantes, t;
    /* Tras un limite_global, lo que le quede a esta conexión no sirve hasta medianoche: los dos contadores del día solo
       crecen. Además, cuota.py gasta la consulta de la IP antes de mirar el total, así que el «te quedan» de antes ya
       ni siquiera es su cupo. Con la fecha, y no con un sí o no, la página abierta pasada la medianoche vuelve a lo normal. */
    if (S.cerrado && S.cerrado === hoyColombia()) t = tt('La demo no admite más consultas hoy.', 'The demo takes no more checks today.');
    else if (n == null) t = tt('3 consultas al día desde cada conexión.', '3 checks a day per connection.');
    else if (n <= 0) t = tt('No te quedan consultas hoy: vuelven a medianoche, hora de Colombia.', 'No checks left today: they reset at midnight, Colombia time.');
    else if (n === 1) t = tt('Te queda 1 consulta hoy.', '1 check left today.');
    else t = tt('Te quedan ' + n + ' consultas hoy.', n + ' checks left today.');
    $('ct-cupo').textContent = t;
  }

  function horasTexto() {
    var hrs = Math.ceil(msHastaMedianoche() / 3600000);
    if (hrs <= 1) return tt('en menos de una hora', 'in under an hour');
    return tt('en unas ' + hrs + ' h', 'in about ' + hrs + ' h');
  }

  /* limite_global llega por dos topes distintos (DISEÑO.md §2), y la Lambda dice cuál en "limite": "consultas", el
     total diario de consultas entre todos, o "peticiones", el presupuesto diario de peticiones a las fuentes. El aviso
     decía «tope de 100 consultas» siempre, y con el presupuesto agotado se habían hecho 34 (reproducido). Ningún número,
     tampoco con "consultas": el contador del total sube también con las consultas que el presupuesto rechaza después,
     así que llegar a él no asegura que se sirvieran 100. Sin el campo (una Lambda anterior) o con uno desconocido, una
     redacción que vale para los dos. Los dos topes son del día de Colombia: medianoche vale en cualquier caso. */
  function limiteGlobal(cual) {
    var que = cual === 'consultas'
      ? tt('La demo llegó hoy a su tope diario de consultas entre todos los visitantes.', 'The demo has reached its daily cap on checks across all visitors.')
      : cual === 'peticiones'
        ? tt('La demo gastó hoy su presupuesto diario de peticiones a las dos Cortes, un tope que existe para no saturar sus servidores.',
          'The demo has used up today’s budget of requests to the two Courts, a cap that exists so as not to overload their servers.')
        : tt('La demo llegó hoy a su límite de uso entre todos los visitantes.', 'The demo has reached today’s usage limit across all visitors.');
    return que + tt(' Vuelve a abrir a medianoche, hora de Colombia (' + horasTexto() + '). Mientras tanto, los ejemplos tienen su informe guardado.',
      ' It reopens at midnight, Colombia time (' + horasTexto() + '). Meanwhile, the samples have their saved report.');
  }

  // Mensajes propios por código de DISEÑO.md §2. El «mensaje» del servidor está en español y no se usa salvo para
  // un código que esta página no conozca: así la interfaz en inglés no mezcla idiomas.
  function mensaje(e) {
    switch (e.codigo) {
      case 'texto_vacio': return tt('No hay texto que verificar: pega un escrito o elige un ejemplo.', 'There is no text to check: paste a brief or pick a sample.');
      case 'texto_largo': return tt('El texto pasa de 5.000 caracteres' + (e.n ? ' (tiene ' + num(e.n) + ')' : '') + '. Recórtalo: basta el fragmento con las citas.',
        'The text is over 5,000 characters' + (e.n ? ' (it has ' + num(e.n) + ')' : '') + '. Trim it: the passage with the citations is enough.');
      // En pasado («el texto que enviaste»): la página no sabe contar providencias como la Lambda, así que el aviso se
      // queda mientras se recorta el texto, y en presente diría algo que quizá ya no es verdad
      case 'demasiadas_citas': return tt('El texto que enviaste cita más de ' + MAX_CITAS + ' providencias distintas, y esta demo no acepta más por texto. Pártelo en trozos, mejor de hasta ' + MAX_PROV +
        ' providencias: es lo que se consulta de cada texto, y las demás saldrían sin confirmar. No se gastó ninguna consulta.',
        'The text you sent cites more than ' + MAX_CITAS + ' different rulings, and this demo takes no more than that per text. Split it into pieces, ideally of up to ' + MAX_PROV +
        ' rulings: that is how many are looked up per text, and the rest would come out unconfirmed. No check was used.');
      case 'limite_ip': return tt('Ya se usaron las 3 consultas de hoy desde esta conexión. El cupo vuelve a medianoche, hora de Colombia (' + horasTexto() + '). Mientras tanto, los ejemplos tienen su informe guardado.',
        'This connection has used today’s 3 checks. They reset at midnight, Colombia time (' + horasTexto() + '). Meanwhile, the samples have their saved report.');
      case 'limite_global': return limiteGlobal(e.limite);
      case 'mal_formada': return tt('El servidor no entendió la petición. Recarga la página e inténtalo de nuevo.', 'The server could not read the request. Reload the page and try again.');
      case 'interno': return tt('Algo falló en el servidor de la demo y no hay informe. Inténtalo de nuevo en unos minutos.', 'Something failed on the demo server and there is no report. Try again in a few minutes.');
      /* 429 sin código nuestro: lo da la propia Function URL, sin invocar la Lambda (por eso no se gastó nada). Con la
         concurrencia reservada en 1 (DISEÑO.md §2), casi siempre es que otra persona está verificando; con el freno
         automático (concurrencia 0) es lo mismo hasta que alguien lo reactive a mano. La página no puede distinguirlos,
         así que no promete que en un minuto vaya a funcionar, y deja el estado de la demo a mano. */
      case 'ocupado': return [tt('Lo más probable es que otra persona esté verificando ahora: la demo atiende una sola verificación a la vez, para no saturar a la Corte. También puede estar en pausa por exceso de uso. No se gastó ninguna consulta. Prueba de nuevo en un par de minutos y, si sigue igual, mira el ',
        'Most likely someone else is checking right now: the demo runs one check at a time, so as not to overload the Court’s servers. It may also be paused for heavy use. No check was used. Try again in a couple of minutes and, if it keeps happening, see the '),
        h('a', { href: '/estado.html' }, tt('estado de la demo', 'demo status')), '.'];
      case 'ruta': return tt('El servidor de la demo no reconoce la dirección: la página puede estar desactualizada. Recárgala.', 'The demo server does not recognise the address: this page may be out of date. Reload it.');
      case 'tiempo': return tt('El servidor no respondió en dos minutos y medio, y se dejó de esperar. Inténtalo más tarde.', 'The server did not answer within two and a half minutes, so the page stopped waiting. Try again later.');
      case 'red': return tt('No se pudo hablar con el servidor de la demo: no hay conexión, o el servidor no respondió o no admitió la petición desde esta página. Los ejemplos siguen funcionando: su informe ya está guardado.',
        'Could not reach the demo server: there is no connection, or the server did not answer or did not accept the request from this page. The samples still work: their report is already saved.');
      case 'sin_api': return tt('La verificación en vivo no está conectada en esta copia de la página. Los ejemplos sí funcionan.', 'Live checking is not connected on this copy of the page. The samples do work.');
      case 'sin_cupo': return tt('Esta consulta no gastó cupo: no había citas que consultar en las fuentes.', 'This check did not use your quota: there were no citations to look up in the sources.');
      default: return tt('Respuesta inesperada del servidor', 'Unexpected response from the server') + (e.status ? ' (HTTP ' + e.status + ')' : '') + '.' +
        (e.mensaje ? ' ' + tt('Dice: ', 'It says (in Spanish): ') + '«' + String(e.mensaje).slice(0, 300) + '»' : '');
    }
  }

  function pintarEstado() {
    var caja = $('ct-estado-caja'), p = $('ct-estado'), e = S.estado, tipo = '';
    p.textContent = '';
    if (S.cargando) {
      add(p, tt('Verificando contra las fuentes oficiales. Puede tardar hasta un minuto: Cotejo espera entre consultas para no saturar a la Corte.',
        'Checking against the official sources. It can take up to a minute: Cotejo waits between requests so as not to overload the Court’s servers.'));
    } else if (e) {
      tipo = e.tipo;
      add(p, mensaje(e));
    } else if (!API) {
      tipo = 'info';
      add(p, mensaje({ codigo: 'sin_api' }));
    } else if (S.abajo) {
      tipo = 'info';
      add(p, [tt('La verificación en vivo parece fuera de servicio ahora mismo (', 'Live checking seems to be down right now ('),
        h('a', { href: '/estado.html' }, tt('ver estado', 'see status')), tt('). Los ejemplos funcionan igual.', '). The samples work all the same.')]);
    }
    caja.className = 'ct-estado' + (tipo === 'error' ? ' is-error' : tipo === 'info' ? ' is-info' : '');
    $('ct-prog').hidden = !S.cargando;
    $('ct-informe').setAttribute('aria-busy', S.cargando ? 'true' : 'false');
    tic();
    boton();
    cupo();
  }
  /* Con la tarjeta fija y más alta que su max-height (el visitante agrandó el área de texto), su final se desplaza por
     dentro y el aviso, que es lo último, quedaría escondido: se baja el desplazamiento de la tarjeta, nunca el de la
     página. Sin tarjeta fija no hay desplazamiento propio y no hace nada. */
  function verEstado() {
    var ci = $('ct-estado-caja').parentNode;
    if (ci.scrollHeight > ci.clientHeight + 1) ci.scrollTop = ci.scrollHeight;
  }
  // El reloj va fuera de la región role=status y con aria-hidden: anunciar cada segundo sería ruido
  function tic() {
    var r = $('ct-reloj');
    if (!S.cargando) { r.textContent = ''; clearInterval(reloj); reloj = 0; return; }
    r.textContent = num(Math.floor((Date.now() - S.t0) / 1000)) + ' s';
    if (!reloj) reloj = setInterval(tic, 1000);
  }
  var anuncio = 0;
  function anunciar(s) {
    var lv = $('ct-live');
    if (!lv) return;
    lv.textContent = '';
    clearTimeout(anuncio);
    anuncio = setTimeout(function () { lv.textContent = s; }, 60);
  }
  function callar() { clearTimeout(anuncio); var lv = $('ct-live'); if (lv) lv.textContent = ''; }

  /* ------------------------------------------------------------ verificación en vivo */
  function codigoDe(x) {
    if (x.j && CODIGOS.indexOf(x.j.error) >= 0) return x.j.error;
    /* La propia Function URL, sin cuerpo nuestro: concurrencia 1 o freno. Un 429 con un "error" que esta página no
       conoce (una Lambda más nueva) es nuestro, la Lambda corrió y puede haber gastado cupo: no es «ocupado», cuyo
       aviso dice que no se gastó nada, sino una respuesta inesperada que enseña su mensaje. */
    if (x.status === 429) return x.j && x.j.error ? 'desconocido' : 'ocupado';
    if (x.status >= 500) return 'interno';
    if (x.status === 404) return 'ruta';
    return 'desconocido';
  }
  // Sin ninguna cita que consultar, la Lambda responde sin gastar cupo (DISEÑO.md §2, paso 3)
  function consultaAlgo(inf) {
    return (inf.hallazgos || []).some(function (x) { return x.estado !== 'no_verificable'; });
  }

  function verificar() {
    // Con el botón «desactivado» por falta de API o por exceso de texto, pulsarlo explica por qué; durante la
    // verificación no hace nada: una segunda petición gastaría otra consulta para el mismo texto
    if (S.cargando) return;
    var texto = $('ct-texto').value, n = largo(texto), clave = S.sel;
    if (!API) { fallo({ codigo: 'sin_api', tipo: 'info' }); return; }
    if (!texto.trim()) { fallo({ codigo: 'texto_vacio' }); return; }
    if (n > MAX) { fallo({ codigo: 'texto_largo', n: n }); return; }
    if (!window.fetch) { fallo({ codigo: 'red' }); return; }
    S.cargando = true; S.t0 = Date.now(); S.estado = null;
    pintarEstado();
    verEstado();
    var ctl = window.AbortController ? new AbortController() : null, vencido = false;
    var tm = setTimeout(function () { vencido = true; if (ctl) ctl.abort(); }, ESPERA_MS);
    fetch(API + 'verificar', {
      method: 'POST', mode: 'cors', credentials: 'omit', cache: 'no-store',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ texto: texto }),
      signal: ctl ? ctl.signal : undefined
    })
      .then(function (r) {
        return r.text().then(function (t) { var j = null; try { j = JSON.parse(t); } catch (e) {} return { ok: r.ok, status: r.status, j: j }; });
      })
      .then(function (x) {
        S.cargando = false;
        if (x.ok && x.j && x.j.informe) exito(x.j, texto, clave);
        else fallo({ codigo: codigoDe(x), status: x.status, mensaje: x.j && x.j.mensaje, n: n, limite: x.j && x.j.limite });
      }, function () {
        // fetch solo rechaza sin respuesta: red caída, CORS que no deja pasar, o el plazo de arriba
        S.cargando = false;
        fallo({ codigo: vencido ? 'tiempo' : 'red' });
      })
      .then(function () { clearTimeout(tm); });
  }

  function exito(j, texto, clave) {
    var ej = EJ[clave] && EJ[clave].texto === texto ? clave : null;
    S.res[clave] = { origen: 'vivo', respuesta: j, texto: texto, cuando: new Date(), ej: ej, clave: clave };
    if (typeof j.restantes === 'number') { S.restantes = j.restantes; guardarCupo(j.restantes); }
    // Una consulta servida que consultó algo quiere decir que el día no está cerrado; una gratuita (solo no
    // verificables) se sirve también con el día cerrado y no dice nada
    if (consultaAlgo(j.informe)) S.cerrado = null;
    S.estado = consultaAlgo(j.informe) ? null : { tipo: 'info', codigo: 'sin_cupo' };
    pintarEstado();
    verEstado();
    var inf = j.informe, hs = inf.hallazgos || [], atn = hs.filter(function (x) { return GRUPOS[0][1].indexOf(x.estado) >= 0; }).length;
    // Si mientras tanto eligió otro texto, el informe no se dibuja: el anuncio dice de cuál es, o parecería el de lo que
    // tiene a la vista
    var otro = S.sel !== clave, de = clave === 'propio' ? tt('tu texto', 'your text') : EJ[clave] ? '«' + nm(EJ[clave].titulo) + '»' : '';
    anunciar(tt('Informe listo' + (otro ? ' para ' + de : '') + ': ' + hs.length + (hs.length === 1 ? ' providencia' : ' providencias') + ', ' + atn + (atn === 1 ? ' requiere' : ' requieren') + ' atención.',
      'Report ready' + (otro ? ' for ' + de : '') + ': ' + hs.length + (hs.length === 1 ? ' ruling' : ' rulings') + ', ' + atn + (atn === 1 ? ' needs' : ' need') + ' attention.'));
    if (!otro) {
      renderInforme();
      /* El foco va al informe solo si nadie lo puso en otro sitio: la espera dura hasta un minuto y lo natural es seguir
         retocando el texto, y lo que se tecleara después de quitarle el foco al área de texto iría al título y se
         perdería. El anuncio de #ct-live ya avisa. Un foco que estaba en el informe anterior ha caído en el body. */
      var a = document.activeElement, hd = $('ct-inf-h');
      if (hd && (!a || a === document.body || a.id === 'ct-verificar')) hd.focus();
    }
  }

  function fallo(e) {
    S.estado = { tipo: e.tipo || 'error', codigo: e.codigo, status: e.status, mensaje: e.mensaje, n: e.n, limite: e.limite };
    if (e.codigo === 'limite_ip') { S.restantes = 0; guardarCupo(0); }
    if (e.codigo === 'limite_global') S.cerrado = hoyColombia();
    pintarEstado();
    verEstado();
  }

  /* ------------------------------------------------------------ informe */
  /* informe._resaltar: marca la primera aparición de cada tramo distinto, como palabra entera. Es solo ayuda visual:
     la diferencia exacta ya va escrita en la observación, así que si un tramo aparece dos veces y se marca el que no
     era, no se pierde información. \p{L} y no \w: el \w de JavaScript no reconoce «también» como palabra. */
  var UNICODE = (function () { try { return new RegExp('(?<![\\p{L}\\p{N}_])a', 'iu').test('a'); } catch (e) { return false; } })();
  function buscarTramo(texto, p) {
    var esc = p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (UNICODE) {
      try {
        var m = new RegExp('(?<![\\p{L}\\p{N}_])' + esc + '(?![\\p{L}\\p{N}_])', 'iu').exec(texto);
        return m ? [m.index, m.index + m[0].length] : null;
      } catch (e) {}
    }
    var i = texto.toLowerCase().indexOf(p.toLowerCase());
    return i < 0 ? null : [i, i + p.length];
  }
  function resaltar(texto, tramos) {
    var marcas = [];
    tramos.forEach(function (p) {
      p = String(p || '').trim();
      if (!p) return;
      var m = buscarTramo(texto, p);
      if (m && marcas.every(function (r) { return m[1] <= r[0] || m[0] >= r[1]; })) marcas.push(m);
    });
    marcas.sort(function (a, b) { return a[0] - b[0]; });
    var out = [], pos = 0;
    marcas.forEach(function (r) {
      if (r[0] > pos) out.push(texto.slice(pos, r[0]));
      out.push(h('mark', null, texto.slice(r[0], r[1])));
      pos = r[1];
    });
    out.push(texto.slice(pos));
    return out;
  }

  function pasaje(p) {
    var caja = h('div', { class: 'ct-pas' }), txt = plano(p.pasaje), frs = p.fragmentos || [];
    if (p.estado === 'coincide') {
      add(caja, [h('span', { class: 'ct-et' }, tt('Pasaje citado · aparece literal en la sentencia', 'Quoted passage · appears word for word in the ruling')),
        h('blockquote', { class: 'ct-q is-real', lang: 'es' }, txt)]);
      return caja;
    }
    if (!frs.length) {
      add(caja, [h('span', { class: 'ct-et' }, tt('Pasaje citado · demasiado corto para compararlo', 'Quoted passage · too short to compare')),
        h('blockquote', { class: 'ct-q', lang: 'es' }, txt)]);
      return caja;
    }
    // El fragmento que peor encaja es el que se enseña, como en el informe impreso (el primero, si empatan)
    var peor = frs.reduce(function (a, f) { return f.puntuacion < a.puntuacion ? f : a; }, frs[0]);
    var difiere = p.estado === 'difiere';
    var todas = [];
    frs.forEach(function (f) { (f.diferencias || []).forEach(function (d) { todas.push(d); }); });
    add(caja, [
      h('span', { class: 'ct-et' }, tt('Pasaje citado · ', 'Quoted passage · ') +
        (difiere ? tt('no coincide literalmente', 'does not match word for word') : tt('no aparece en la sentencia', 'does not appear in the ruling')) +
        ' · ' + pct(p.puntuacion) + tt(' de las palabras', ' of the words match')),
      h('blockquote', { class: 'ct-q', lang: 'es' }, difiere ? resaltar(txt, todas.map(function (d) { return d.escrito; })) : txt)
    ]);
    if (peor.mas_parecido) {
      var real = plano(peor.mas_parecido);
      add(caja, [
        h('span', { class: 'ct-et' }, difiere ? tt('Lo que dice la sentencia en ese punto', 'What the ruling says at that point')
          : tt('Lo más parecido que dice la sentencia', 'The closest the ruling comes')),
        h('blockquote', { class: 'ct-q is-real', lang: 'es' },
          difiere ? resaltar(real, (peor.diferencias || []).map(function (d) { return d.sentencia; })) : real)
      ]);
    }
    return caja;
  }

  function busquedaCSJ(clave, sala) {
    return BUSCADOR_CSJ + '#/resultados-busqueda/%22' + encodeURIComponent(clave) + '%22/' + sala;
  }

  function tarjeta(x) {
    var e = estadoValido(x.estado), como = [];
    (x.citado_como || []).forEach(function (c) { c = plano(c); if (c && como.indexOf(c) < 0) como.push(c); });
    var veces = (x.citado_como || []).length;
    var art = h('article', { class: 'ct-h is-' + e }, h('div', { class: 'ct-cab' }, [
      h('h5', { lang: 'es' }, como[0] || x.clave),
      h('span', { class: 'ct-chip is-' + e }, lt(ETQ[e])),
      veces > 1 ? h('span', { class: 'ct-veces' }, tt('citada ' + veces + ' veces', 'cited ' + veces + ' times')) : null
    ]));

    var d = [], p = x.providencia;
    if (p) {
      // El identificador oficial solo si el escrito la citó de otra forma («T-406 de 1992» frente a «T-406/92»)
      if (p.id && p.id !== como[0]) d.push(h('span', { class: 'id' }, p.id));
      if (p.fecha) d.push(fecha(p.fecha));
      if (p.ponente) d.push(h('span', null, [tt('M.P. ', 'Reporting justice: '), h('span', { lang: 'es' }, p.ponente)]));
      d.push(enlace(p.url, x.fuente === 'corte_suprema' ? tt('texto en la Corte Suprema ↗', 'text at the Supreme Court ↗') : tt('texto en la relatoría ↗', 'text at the Court’s reporter ↗')));
      if (x.fuente === 'relatoria') d.push(tt('aún no figura en el índice oficial', 'not in the official index yet'));
    } else if (x.tribunal) {
      d.push(lt(TRIBUNAL[x.tribunal] || x.tribunal));
      var m = /^([A-Z]+)\d+-\d{4}$/.exec(x.clave || '');
      if (x.tribunal === 'Corte Suprema de Justicia' && m && SALAS[m[1]]) {
        d.push(enlace(busquedaCSJ(x.clave, SALAS[m[1]]), tt('buscarla en la Corte Suprema ↗', 'search for it at the Supreme Court ↗')));
      }
    }
    if (d.length) art.appendChild(h('p', { class: 'ct-datos' }, unir(d, ' · ')));
    if (como.length > 1) {
      art.appendChild(h('p', { class: 'ct-como' }, [tt('Citada como: ', 'Cited as: '),
        h('span', { lang: 'es' }, como.map(function (c) { return '«' + c + '»'; }).join(', '))]));
    }

    var notas = [];
    if (x.motivo) notas.push(h('li', { lang: 'es' }, x.motivo));
    (x.observaciones || []).forEach(function (o) { notas.push(h('li', { lang: 'es' }, o)); });
    if (x.posibles && x.posibles.length) {
      notas.push(h('li', { class: 'is-sug' }, tt('¿Quizá quería citar ', 'Did you mean to cite ') + x.posibles.join(', ') + '?'));
    }
    if (notas.length) art.appendChild(h('ul', { class: 'ct-obs' }, notas));
    (x.pasajes || []).forEach(function (ps) { art.appendChild(pasaje(ps)); });
    return art;
  }

  function comparar(res) {
    var g = guardado(res.ej);
    if (!g) return null;
    var a = {}, b = {}, claves = [], cambios = [];
    (g.respuesta.informe.hallazgos || []).forEach(function (x) { a[x.clave] = x.estado; claves.push(x.clave); });
    (res.respuesta.informe.hallazgos || []).forEach(function (x) { b[x.clave] = x.estado; if (!(x.clave in a)) claves.push(x.clave); });
    claves.forEach(function (k) {
      if (a[k] !== b[k]) cambios.push(k + ': ' + (a[k] ? lt(ETQ[estadoValido(a[k])]) : '—') + ' → ' + (b[k] ? lt(ETQ[estadoValido(b[k])]) : '—'));
    });
    return cambios;
  }

  function peticiones(pt, inf) {
    // Sin nada que consultar (ninguna cita, o solo no verificables) la Lambda no pregunta a nadie ni mira la caché: el
    // 0 y 0 no quiere decir «de la caché», y el aviso de encima ya dice que no había nada que consultar
    if (!consultaAlgo(inf)) return null;
    pt = pt || {};
    var r = pt.relatoria || 0, c = pt.corte_suprema || 0;
    if (!r && !c) {
      // Tampoco con un «sin confirmar»: con el cortacircuitos abierto, o por el tiempo o el tope, una cita sale sin
      // ninguna petición y sin haber salido de la caché
      var sc = (inf.hallazgos || []).some(function (x) { return estadoValido(x.estado) === 'sin_confirmar'; });
      return sc ? tt('ninguna petición nueva a las fuentes', 'no new requests to the sources')
        : tt('ninguna petición nueva a las fuentes: todo salió de la caché', 'no new requests to the sources: everything came from the cache');
    }
    return tt('peticiones a las fuentes: ' + r + ' a la relatoría y ' + c + ' a la Corte Suprema',
      'requests to the sources: ' + r + ' to the reporter and ' + c + ' to the Supreme Court');
  }

  function informe(res) {
    var j = res.respuesta, inf = j.informe || {}, hs = inf.hallazgos || [], ta = $('ct-texto');
    // Un ejemplo editado y verificado en vivo sigue siendo de su ejemplo, pero su informe no es el del ejemplo
    var titulo = EJ[res.clave] ? nm(EJ[res.clave].titulo) + (res.ej ? '' : tt(' (editado)', ' (edited)')) : tt('Tu texto', 'Your text');
    var sec = h('section', { class: 'ct-inf', 'aria-labelledby': 'ct-inf-h' });

    var origen = res.origen === 'vivo'
      ? tt('Informe en vivo: verificado hoy a las ', 'Live report: checked today at ') + hora(res.cuando) +
        (typeof j.segundos === 'number' ? tt(' en ', ' in ') + num(j.segundos, 1) + ' s' : '') + '.'
      : tt('Informe guardado, verificado el ', 'Saved report, checked on ') + fecha(D && D.generado) +
        (D && D.commit ? tt(' (commit ' + D.commit + ' de Cotejo)', ' (Cotejo commit ' + D.commit + ')') : '') + '. ' +
        tt('«Verificar en vivo» lo repite ahora contra las fuentes y gasta una consulta.', '“Check live” repeats it now against the sources and uses one check.');
    var ix = j.indice || {};
    var datos = unir([
      hs.length + (hs.length === 1 ? tt(' providencia citada', ' ruling cited') : tt(' providencias citadas', ' rulings cited')),
      typeof inf.palabras === 'number' ? num(inf.palabras) + tt(' palabras', ' words') : null,
      ix.providencias ? tt('índice oficial: ', 'official index: ') + num(ix.providencias) + tt(' providencias', ' rulings') +
        (ix.hasta ? tt(', hasta el ', ', up to ') + fecha(ix.hasta) : '') : null,
      peticiones(j.peticiones, inf)
    ], ' · ');
    add(sec, h('div', { class: 'ct-inf-cab' }, [
      h('h3', { id: 'ct-inf-h', tabindex: '-1' }, tt('Informe · ', 'Report · ') + titulo),
      h('p', { class: 'note ct-meta' }, origen),
      h('p', { class: 'note ct-meta' }, datos)
    ]));

    S.viejo = res.texto !== ta.value;
    if (S.viejo) {
      add(sec, h('p', { class: 'ct-aviso is-warn' }, res.origen === 'guardado'
        ? tt('Cambiaste el texto: este informe es del ejemplo tal como venía. «Verificar en vivo» verifica el texto nuevo.',
          'You changed the text: this report is for the sample as it was. “Check live” checks the new text.')
        : tt('Cambiaste el texto después de verificarlo: este informe es del texto anterior.',
          'You changed the text after checking it: this report is for the previous text.')));
    }
    if (j.cortado_por_tiempo) {
      add(sec, h('p', { class: 'ct-aviso is-warn' }, tt('La verificación llegó a su tiempo máximo: las citas que no se alcanzaron a consultar salen «sin confirmar», con ese motivo.',
        'The check reached its time limit: the citations it did not get to come out “unconfirmed”, with that reason.')));
    }
    if (j.cortado_por_tope) {
      add(sec, h('p', { class: 'ct-aviso is-warn' }, tt('Esta demo consulta como mucho ' + MAX_PROV + ' providencias por texto; las demás salen «sin confirmar», con ese motivo. Para verificarlas, pégalas aparte.',
        'This demo looks up at most ' + MAX_PROV + ' rulings per text; the rest come out “unconfirmed”, with that reason. To check them, paste them separately.')));
    }
    if (res.origen === 'vivo' && res.ej) {
      var cambios = comparar(res);
      if (cambios && !cambios.length) {
        add(sec, h('p', { class: 'ct-aviso is-ok' }, tt('Mismo resultado que el informe guardado, providencia por providencia.', 'Same result as the saved report, ruling by ruling.')));
      } else if (cambios) {
        add(sec, h('p', { class: 'ct-aviso is-warn' }, tt('Cambió respecto al informe guardado: ', 'Changed from the saved report: ') + cambios.join('; ') +
          tt('. El motivo está en cada providencia.', '. Each ruling gives the reason.')));
      }
    }
    if (en()) {
      add(sec, h('p', { class: 'ct-aviso' }, 'Cotejo writes its reasons and observations in Spanish, like the rulings and passages it quotes; only the interface is translated.'));
    }

    var r = inf.resumen || {};
    add(sec, h('div', { class: 'ct-resumen', role: 'list', 'aria-label': tt('Resumen por estado', 'Summary by status') }, ORDEN.map(function (e) {
      var n = r[e] || 0;
      return h('div', { class: 'ct-cifra is-' + e + (n ? '' : ' is-cero'), role: 'listitem' }, [h('b', null, num(n)), h('span', null, lt(ETQ[e]))]);
    })));

    if (!hs.length) {
      add(sec, h('div', { class: 'ct-vacio' }, h('p', null, tt('No se encontró ninguna cita de jurisprudencia en el texto. Cotejo reconoce citas como «Sentencia T-406 de 1992», «T-760/08» o «SC5686-2018».',
        'No case-law citation was found in the text. Cotejo recognises citations such as «Sentencia T-406 de 1992», «T-760/08» or «SC5686-2018».'))));
    }
    GRUPOS.forEach(function (g) {
      // Las tarjetas llegan ya en el orden de informe._ordenados; aquí solo se reparten por grupo
      var xs = hs.filter(function (x) { return g[1].indexOf(estadoValido(x.estado)) >= 0; });
      if (!xs.length) return;
      add(sec, h('div', { class: 'ct-grupo' }, [h('h4', null, lt(g[0]) + ' · ' + xs.length)].concat(xs.map(tarjeta))));
    });
    return sec;
  }

  function renderInforme() {
    var c = $('ct-informe'), r = actual();
    c.textContent = '';
    S.mostrado = r;
    if (!r) {
      S.viejo = false;
      add(c, h('div', { class: 'ct-vacio' }, h('p', null, S.sel === 'propio'
        ? tt('Aquí aparecerá el informe. Pega un texto y pulsa «Verificar en vivo».', 'The report will appear here. Paste a text and press “Check live”.')
        : tt('Este ejemplo no trae informe guardado.', 'This sample has no saved report.'))));
      return;
    }
    c.appendChild(informe(r));
  }

  /* ------------------------------------------------------------ textos que dependen de los datos */
  function pintarFuente() {
    var s = $('ct-src');
    if (!s || !D) return;
    s.textContent = '';
    add(s, [tt('Ejemplos: ', 'Samples: '), h('a', { href: '/data/cotejo/ejemplos.json' }, 'ejemplos.json'),
      tt(', verificados el ', ', checked on ') + fecha(D.generado) +
      (D.commit ? tt(' (commit ' + D.commit + ' de Cotejo)', ' (Cotejo commit ' + D.commit + ')') : '') +
      tt(' y exportados por demo/exportar_ejemplos.py. Las cifras de «Lo medido» salen de los archivos de evaluación que se citan en cada una.',
        ' and exported by demo/exportar_ejemplos.py. The figures under “What was measured” come from the evaluation files named under each one.')]);
  }
  // Tamaño y fecha del índice que usó Cotejo, tomados del primer ejemplo: una cifra escrita a mano envejecería
  function pintarIndice() {
    var e = D && D.ejemplos[0], ix = e && e.respuesta && e.respuesta.indice;
    if (!ix || !ix.providencias) return;
    [].slice.call(document.querySelectorAll('.ct-idx')).forEach(function (s) {
      var l = s.getAttribute('data-idx');
      s.textContent = l === 'en'
        ? ' (' + num(ix.providencias, 0, 'en') + ' rulings' + (ix.hasta ? ', up to ' + fecha(ix.hasta, 'en') : '') + ')'
        : ' (' + num(ix.providencias, 0, 'es') + ' providencias' + (ix.hasta ? ', hasta el ' + fecha(ix.hasta, 'es') : '') + ')';
    });
  }
  function pintarPlaceholder() {
    var ta = $('ct-texto'), ph = ta.getAttribute('data-ph-' + L.lang());
    if (ph) ta.setAttribute('placeholder', ph);
  }

  /* ------------------------------------------------------------ estado en la URL (#ej=…) para compartir un ejemplo */
  function leerHash() {
    var m = /(?:^#|&)ej=([a-z_]+)/.exec(location.hash);
    return m && EJ[m[1]] ? m[1] : null;
  }
  function escribirHash() {
    if (!tocado) return;
    try { history.replaceState(null, '', S.sel === 'propio' ? location.pathname + location.search : '#ej=' + S.sel); } catch (e) {}
  }

  /* ------------------------------------------------------------ arranque */
  function cargar(n) {
    return fetch('/data/cotejo/ejemplos.json', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .catch(function (e) {
        if (n > 0) return new Promise(function (ok) { setTimeout(ok, 1200); }).then(function () { return cargar(n - 1); });
        throw e;
      });
  }

  function start() {
    API = resolverApi();
    S.restantes = leerCupo();
    var ta = $('ct-texto'), pick = $('ct-pick');
    pick.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-ej]');
      if (b) elegir(b.getAttribute('data-ej'), true);
    });
    pick.addEventListener('keydown', pickKeys);
    ta.addEventListener('input', alEscribir);
    $('ct-verificar').addEventListener('click', verificar);
    $('ct-restaurar').addEventListener('click', restaurar);
    // Un enlace #ej=… dentro de la misma página no la recarga: sin esto, el ejemplo enlazado no se abriría
    window.addEventListener('hashchange', function () { var id = leerHash(); if (id && id !== S.sel) elegir(id, false); });
    L.onLang(function () {
      // El último anuncio se oyó en su idioma; si se quedara, un lector de pantalla en modo exploración lo leería con
      // la voz del idioma nuevo (la región no lleva lang propio)
      callar();
      renderPick();
      pintarPlaceholder();
      contar();
      pintarEstado();
      renderInforme();
      pintarFuente();
    });
    if (L.demoDown) L.demoDown('cotejo', function (down) { if (down) { S.abajo = true; pintarEstado(); } });

    pintarPlaceholder();
    marcarPick();
    contar();
    pintarEstado();
    renderInforme();

    cargar(1).then(function (d) {
      if (!d || !Array.isArray(d.ejemplos)) throw new Error('ejemplos.json sin ejemplos');
      D = d;
      D.ejemplos.forEach(function (e) { EJ[e.id] = e; });
      $('ct-carga').hidden = true;
      renderPick();
      pintarFuente();
      pintarIndice();
      // Si el visitante ya empezó a escribir mientras cargaba, su texto manda
      if (!tocado && !ta.value && D.ejemplos.length) {
        var id = leerHash() || D.ejemplos[0].id;
        S.sel = id;
        ta.value = EJ[id].texto;
        marcarPick();
        contar();
        renderInforme();
      }
    }).catch(function (e) {
      if (window.console) console.error(e);
      // En los dos idiomas a la vez: el aviso no se vuelve a dibujar al cambiar de idioma
      var c = $('ct-carga');
      c.textContent = '';
      add(c, [h('span', { 'data-l': 'es' }, 'No se pudieron cargar los ejemplos. Puedes pegar tu propio texto, o recargar la página.'),
        h('span', { 'data-l': 'en' }, 'The samples could not be loaded. You can paste your own text, or reload the page.')]);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
