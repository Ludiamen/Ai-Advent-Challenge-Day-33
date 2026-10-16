// Сквозная проверка страницы в настоящем браузере.
//
// Зачем отдельно от tests.py: тесты на Python проверяют сервер и структуру
// скрипта, но не могут выполнить страницу. Ошибка, ради которой этот прогон и
// появился, была именно такой — «запуститьСценарий» оказался объявлен внутри
// другой функции. Синтаксис корректен, сервер отвечает, все 150 тестов зелёные,
// а кнопка «Спросить» падает с ReferenceError, и сценарий не запускается вовсе.
//
// Как запускать:
//   1) поднять сервер:   MEMORY_DIR=/tmp/проба PORT=5000 python web.py
//   2) поднять браузер:  google-chrome --headless=new --remote-debugging-port=9222 \
//                          --no-sandbox --disable-gpu about:blank
//   3) сам прогон:       node browser.mjs [адрес] [ключ-модели] [запрос] [режим]
//
// Режимы:
//   вопрос (по умолчанию)   — задать агенту вопрос с главной и дождаться ответа;
//   переходы                — завести задачу, нажать запертую кнопку перехода и
//                             убедиться, что страница показывает разбор отказа,
//                             а после плана и утверждения переход открывается;
//   mcp                     — панель MCP: подключиться к своему серверу, затем ко
//                             всем; проверить карточки (соединение, сбой, пропуск),
//                             пометки доступа, закрытые фильтром инструменты и
//                             JSON-схему. Модель в этом режиме не вызывается.
//   инструменты             — День 17: включить инструменты, задать вопрос про
//                             трекер и увидеть строки вызовов «↳» под ответом;
//                             попросить изменение и убедиться, что появилась
//                             заявка, а вызова не было; подтвердить её и увидеть
//                             результат; вызвать инструмент кнопкой из панели MCP.
//                             Нужен поднятый мок: python tracker_api.py
//   индекс                  — День 21: страница /rag. Таблица документов, поиск
//                             двумя стратегиями рядом, чанк с границами соседей,
//                             сравнение стратегий кнопкой, отказ на неверные
//                             параметры сборки. Индекс должен быть собран.
//   rag                     — День 22: карточки «Вопрос по документам» и
//                             «Контрольные вопросы» на /rag: два ответа рядом,
//                             источники и фрагменты, ловушка, прогон десяти
//                             вопросов с таблицей. Нужны индекс и ключ модели.
//   реранк                  — День 23: карточки «Поиск в два этапа» и «Подбор
//                             порога и top-K» на /rag. Режимы из rag-settings.json,
//                             отбор по этапам в базе и с фильтром (реранкер
//                             поднимает п. 2.2.7 из-за пределов top-5), rewrite,
//                             порог и K вживую, ответы в четырёх режимах и
//                             контрольные вопросы в пяти. Нужны индекс, подбор
//                             и ключ модели.
//   цитаты                  — День 24: карточки «Ответ с источниками и цитатами»,
//                             «Проверка цитат» и «Порог «не знаю»» на /rag: ответ
//                             с цитатами и подсветкой во фрагменте, «не знаю» по
//                             порогу и по модели, поправка порога, формат Дня 23,
//                             проверка на вопросах, подбор порога. Нужны индекс,
//                             подбор Дня 23 и ключ модели.
//   чат                     — День 25: страница /chat. Пять реплик в своей сессии:
//                             цель и ответ с источником и цитатой, «у них» по
//                             истории, ограничение в памяти задачи, ловушка с
//                             «не знаю» без потери цели, ответ по памяти с
//                             «выяснено»; история после перезагрузки, новый
//                             разговор, два длинных сценария кнопкой. Нужны
//                             индекс, подбор Дня 23 и ключ модели.
//   локально                — День 26: страница /local. Служба, петлевой адрес и
//                             модели Ollama; простой вопрос «оба рядом» (локально
//                             и облако), модель в памяти на CPU, средний вопрос;
//                             CLI, родной /api/chat и OpenAI /v1 — ответ 391;
//                             таблица замера и отметка 👍; сырой /api/tags.
//                             Нужны Ollama со скачанными моделями и ключ облака.
//   офлайн                  — День 27: офлайн-режим всего приложения. Переключатель
//                             в полосе включает его; на главной облачные модели
//                             серые; на /local карта ролей (14 ролей → локальные
//                             модели), поиск кросс-энкодером, серверы MCP без
//                             интернета; в чате реплика идёт по этапам со
//                             счётчиком токенов, ответ с источником и цитатой,
//                             внешних запросов 0; выключение офлайна.
//                             Нужны Ollama и индекс; облачный ключ не нужен.
//   офлайн-инструменты      — День 27: в офлайне на главной вопрос агенту с
//                             инструментами трекера: строка хода (круг на
//                             loc-qwen35, вызов, счётчик токенов), строка вызова
//                             «↳ tracker__…», ответ по данным, внешних 0.
//                             Нужен поднятый мок: python tracker_api.py
//   локальный-rag           — День 28: страница /rag в офлайне. Вопрос c01 из
//                             набора → ход по этапам (поиск с разбивкой, ответ
//                             loc-qwen35 со счётчиком токенов) → ответ «20 числа»
//                             с источником и дословной цитатой, замеры по слоям,
//                             внешних запросов 0. Нужны Ollama и индекс.
//   сравнение               — День 28: «Рядом с облаком» на c01. В офлайне —
//                             понятный отказ; в облачном режиме — четыре колонки
//                             (лок-4b, лок-1.7b, облако на тех же фрагментах и на
//                             лучшем конвейере), у локальных внешних 0, у
//                             облачных больше 0; отметка 👍 сохраняется; карточка
//                             сводки сравнения. Нужны Ollama, индекс и ключ облака.
//   оптимизация             — День 29: карточка «Оптимизация под задачу» на
//                             /local: 15+ конфигураций, все веса в Ollama; второй
//                             сервер стартует с KV-кэшем q8_0 и останавливается;
//                             c01 в «до» и «после» — оба верно, память раннера и
//                             секунды процессора, внешних 0; таблица скрининга и
//                             смета VPS. Нужны Ollama, индекс и скрининг.
//   показ                   — День 29 для показа и записи экрана: сцены
//                             DEMO-CHROME.md. Ожидания модели пишутся в файл
//                             MARKS — по нему demo_speedup.py собирает ускоренную
//                             копию.
//
// Панели Дней 11–20 (профиль, сценарии, пауза, планировщик, конвейеры, оркестр)
// в Дне 28 убраны из веба — их режимы здесь удалены, проверка — в check_cli.py.
//
// По умолчанию: http://127.0.0.1:5000, модель ds-flash. Прогон печатает, что
// появляется в чате, и — главное — исключения JS, которых в чате не видно.
//
// Ловушка, которая уже стоила времени: выражения для страницы собираются
// шаблонными строками, а в них «\s» — это не regexp-класс, а просто буква «s»
// (JS молча съедает неизвестный escape). «replace(/\s+/g, ' ')» внутри такого
// шаблона вырезал из текста все «s»: «tracker__list_issues» печатался как
// «tracker__li t_i ue », и проверка честно не находила задание. Внутри
// шаблонов escape удваивается: «/\\s+/g».

const АДРЕС = process.argv[2] || 'http://127.0.0.1:5000';
const МОДЕЛЬ = process.argv[3] || 'ds-flash';
const РЕЖИМ = process.argv[5] || 'вопрос';
const ЗАПРОС = process.argv[4] ||
  'Напиши фичу по получению данных о кадастровых участках с ресурса ' +
  'https://nspd.gov.ru/map и вывода их отдельным слоем на карте ГИС с газопроводами.';
const CDP = process.env.CDP || 'http://127.0.0.1:9222';
const ПРЕДЕЛ_МС = 300000;

const цель = await (await fetch(`${CDP}/json/new?${encodeURIComponent(АДРЕС)}`,
                                {method: 'PUT'})).json();
const ws = new WebSocket(цель.webSocketDebuggerUrl);
const ждущие = new Map();
const ошибки = [];
let счётчик = 0;

ws.addEventListener('message', (событие) => {
  const м = JSON.parse(событие.data);
  if (м.id && ждущие.has(м.id)) { ждущие.get(м.id)(м); ждущие.delete(м.id); }
  if (м.method === 'Runtime.exceptionThrown') {
    const д = м.params.exceptionDetails;
    ошибки.push('исключение: ' + (д.exception?.description || д.text));
  }
  if (м.method === 'Runtime.consoleAPICalled' && м.params.type === 'error') {
    ошибки.push('console.error: ' +
                м.params.args.map(а => а.value ?? а.description).join(' '));
  }
});
await new Promise(р => ws.addEventListener('open', р));

// Ответ CDP может не прийти совсем (страница подвисла под нагрузкой машины): без предела
// ожидание вечное — так встал показ Telegram-бота на сцене 3. После предела — пустой ответ и
// запись в ошибки; по умолчанию 10 минут (в режимах прошлых дней бывают долгие вызовы).
const ПРЕДЕЛ_CDP_МС = Number(process.env.CDP_TIMEOUT || 600000);
const зов = (метод, параметры = {}) => new Promise(р => {
  const id = ++счётчик;
  const таймер = setTimeout(() => {
    if (ждущие.delete(id)) { ошибки.push(`CDP: нет ответа ${ПРЕДЕЛ_CDP_МС / 1000} с на ${метод}`); р({}); }
  }, ПРЕДЕЛ_CDP_МС);
  ждущие.set(id, м => { clearTimeout(таймер); р(м); });
  ws.send(JSON.stringify({id, method: метод, params: параметры}));
});

const выполнить = async (код) => {
  const о = await зов('Runtime.evaluate',
                      {expression: код, awaitPromise: true, returnByValue: true});
  if (о.result?.exceptionDetails) ошибки.push('evaluate: ' + о.result.exceptionDetails.text);
  return о.result?.result?.value;
};

await зов('Runtime.enable');
await зов('Log.enable');
// Страница подтягивает состояние. Ждём загрузку, а не фиксированные 3 с: под нагрузкой машины
// (локальная модель на CPU) страница не успевала, заголовок был пуст, а переход на /chat
// терялся (День 31) — водитель ждал ответов на чужой странице.
for (let н = Date.now(); Date.now() - н < 60000; ) {
  if (await выполнить(`document.readyState === 'complete' && !!document.title`)) break;
  await new Promise(р => setTimeout(р, 500));
}
await new Promise(р => setTimeout(р, 1500));

// Запись страницы через CDP (День 31): кадры отдаёт сам Chrome (Page.startScreencast), поэтому
// в запись попадает только страница — чужое окно не может закрыть её даже на секунду. Захват
// области экрана (x11grab) в первую секунду показа поймал окно редактора под ещё не поднятым
// Chrome. RECORD_CDP — путь .mp4; рядом — .start с моментом начала (для меток ускорения).
let остановитьЗапись = async () => {};
// Telegram Web первые секунды показывает список чатов владельца: там запись начинается только
// после того, как список скрыт (режимы «телеграм…» зовут начатьЗапись сами).
const начатьЗапись = async () => {
  if (!process.env.RECORD_CDP || остановитьЗапись.идёт) return;
  const {spawn} = await import('node:child_process');
  const fs = await import('node:fs');
  const выход = process.env.RECORD_CDP;
  const ffmpeg = spawn('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-f', 'image2pipe', '-framerate', '10', '-i', '-',
    '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
    '-pix_fmt', 'yuv420p', выход], {stdio: ['pipe', 'ignore', 'inherit']});
  let кадр = null, кадров = 0;
  ws.addEventListener('message', (событие) => {
    const м = JSON.parse(событие.data);
    if (м.method !== 'Page.screencastFrame') return;
    кадр = Buffer.from(м.params.data, 'base64');
    ws.send(JSON.stringify({id: ++счётчик, method: 'Page.screencastFrameAck', params: {sessionId: м.params.sessionId}}));
  });
  await зов('Page.enable');
  await зов('Page.startScreencast', {format: 'jpeg', quality: 85, everyNthFrame: 1});
  for (let н = Date.now(); !кадр && Date.now() - н < 10000; ) await new Promise(р => setTimeout(р, 100));
  fs.writeFileSync(выход + '.start', String(Date.now()));
  const тик = setInterval(() => { if (кадр && ffmpeg.stdin.writable) { ffmpeg.stdin.write(кадр); кадров++; } }, 100);
  остановитьЗапись = async () => {
    clearInterval(тик);
    await зов('Page.stopScreencast');
    await new Promise(р => { ffmpeg.on('close', р); ffmpeg.stdin.end(); });
    console.log(`запись страницы: ${выход} · кадров ${кадров}`);
  };
  остановитьЗапись.идёт = true;
};
if (!РЕЖИМ.startsWith('телеграм')) await начатьЗапись();

console.log('страница:', await выполнить('document.title'));
// На страницах сервиса Дня 30 выбора модели нет: модель у сервиса одна.
if (!['сервис', 'показ', 'телеграм', 'телеграм31', 'телеграм32', 'показ32'].includes(РЕЖИМ)) console.log('модель:', await выполнить(
  `(() => { const с = document.getElementById('модель');
            с.value = ${JSON.stringify(МОДЕЛЬ)};
            с.dispatchEvent(new Event('change'));
            return с.value; })()`));

if (РЕЖИМ === 'mcp') {
  const карточки = () => выполнить(`Array.from(document.querySelectorAll('.mcp-сервер')).map(к => ({
    имя: к.dataset['сервер'], класс: к.className.replace('mcp-сервер', '').trim() || '—',
    инструментов: к.querySelectorAll('.mcp-инструмент').length,
    закрыто: к.querySelectorAll('.mcp-инструмент.закрыт').length,
    только_чтение: к.querySelectorAll('.доступ.только-чтение').length,
    текст: к.innerText.split('\\n').slice(0, 4).join(' | ').slice(0, 160) }))`);
  const ждать = async (секунд) => {
    const начало = Date.now();
    while (Date.now() - начало < секунд * 1000) {
      await new Promise(р => setTimeout(р, 1000));
      const идёт = await выполнить(`document.querySelector('#mcp-панель').innerText.includes('Подключаюсь')`);
      if (!идёт) return (Date.now() - начало) / 1000;
    }
    ошибки.push('панель MCP не дождалась конца подключения');
    return -1;
  };

  const до = await карточки();
  console.log('серверов на панели:', до.length, до.map(к => к.имя).join(', '));
  if (до.length < 2) ошибки.push('на панели MCP нет серверов');

  await выполнить(`document.querySelector('.mcp-подключить[data-mcp="agent-state"]').click(); 'ок'`);
  console.log('agent-state подключён за', await ждать(60), 'с');
  const свой = (await карточки()).find(к => к.имя === 'agent-state');
  console.log('agent-state:', JSON.stringify(свой));
  if (!свой || свой.класс !== 'ок') ошибки.push('свой сервер не подключился');
  if (свой && свой.инструментов !== 6) ошибки.push(`у своего сервера ${свой?.инструментов} инструментов вместо 6`);
  if (свой && свой.только_чтение !== 6) ошибки.push('не все инструменты своего сервера помечены «только чтение»');

  // Схема раскрывается и содержит JSON Schema.
  // Список инструментов свёрнут: раскрываем его так же, как человек, — щелчком.
  await выполнить(`document.querySelector('.mcp-список[data-список="agent-state"] summary').click(); 'ок'`);
  const раскрыт = await выполнить(`document.querySelector('.mcp-список[data-список="agent-state"]').open`);
  if (!раскрыт) ошибки.push('список инструментов не раскрывается');
  const схема = await выполнить(`(() => {
    const д = document.querySelector('.mcp-сервер[data-сервер="agent-state"] .mcp-инструмент[data-инструмент="get_task"] details');
    if (!д) return 'нет';
    д.querySelector('summary').click(); return д.querySelector('pre').innerText; })()`);
  console.log('схема get_task:', (схема || '').replace(/\s+/g, ' ').slice(0, 140));
  if (!(схема || '').includes('"task_id"')) ошибки.push('JSON-схема get_task не показана');

  await выполнить(`document.getElementById('mcp-все').click(); 'ок'`);
  console.log('все серверы осмотрены за', await ждать(240), 'с');
  const после = await карточки();
  const остался = await выполнить(`document.querySelector('.mcp-список[data-список="agent-state"]').open`);
  console.log('раскрытый список пережил повторное подключение:', остался ? 'да' : 'НЕТ');
  if (!остался) ошибки.push('после «Подключиться ко всем» раскрытый список свернулся');
  for (const к of после) console.log(`  ${к.имя.padEnd(12)} ${к.класс.padEnd(9)} инструментов ${к.инструментов}, закрыто ${к.закрыто} | ${к.текст}`);
  const фс = после.find(к => к.имя === 'filesystem');
  if (фс && фс.класс === 'ок' && фс.закрыто === 0) ошибки.push('фильтр filesystem не отмечен на странице');
  const гх = после.find(к => к.имя === 'github');
  if (гх && !['пропущен', 'ок'].includes(гх.класс)) ошибки.push('github показан не так, как настроен');

  const итог = await выполнить(`Array.from(document.querySelectorAll('#чат .системное'))
    .map(у => у.innerText).filter(т => т.startsWith('MCP:')).pop() || ''`);
  console.log('\nв чате:', итог);
  if (!итог) ошибки.push('в чате нет итога подключения');
  const сбоиMCP = await выполнить(`document.querySelectorAll('#чат .ошибка').length`);
  console.log('\n=== итог ===');
  console.log('сообщений об ошибке в чате:', сбоиMCP);
  console.log('исключений JS и провалов:', ошибки.length ? ошибки : 'нет');
  ws.close();
  process.exit(ошибки.length || сбоиMCP ? 1 : 0);
}

if (РЕЖИМ === 'инструменты') {
  const чат = () => выполнить(`Array.from(document.querySelectorAll('#чат .msg'))
    .map(у => (у.className.replace('msg','').trim() + ': ' + у.innerText).slice(0, 200))`);
  // Заявка появляется не в переписке, а карточкой справа: ждать её надо по
  // узлу, а не по тексту чата.
  const ждатьУзел = async (селектор, секунд) => {
    const начало = Date.now();
    while (Date.now() - начало < секунд * 1000) {
      await new Promise(р => setTimeout(р, 1000));
      const есть = await выполнить(`!!document.querySelector(${JSON.stringify(селектор)})`);
      if (есть) return (Date.now() - начало) / 1000;
    }
    ошибки.push(`не дождались узла: ${селектор}`);
    return -1;
  };
  const ждатьЧат = async (признак, секунд) => {
    const начало = Date.now();
    while (Date.now() - начало < секунд * 1000) {
      await new Promise(р => setTimeout(р, 1000));
      const есть = await выполнить(
        `document.getElementById('чат').innerText.includes(${JSON.stringify(признак)})`);
      if (есть) return (Date.now() - начало) / 1000;
    }
    ошибки.push(`в чате не дождались: ${признак}`);
    return -1;
  };

  // 1. Включаем инструменты и ждём, пока страница скажет, во что это обходится.
  await выполнить(`(() => {
    document.getElementById('инструменты-серверы').value = 'tracker';
    const г = document.getElementById('инструменты-вкл');
    г.checked = true; г.dispatchEvent(new Event('change')); return 'ок'; })()`);
  const предел = Date.now() + 90000;
  let итогВключения = '';
  while (Date.now() < предел) {
    await new Promise(р => setTimeout(р, 1000));
    итогВключения = await выполнить(`document.getElementById('инструменты-итог').innerText`);
    if (итогВключения && !итогВключения.includes('подключаюсь')) break;
  }
  console.log('инструменты включены:', итогВключения);
  if (!/инстр\./.test(итогВключения)) ошибки.push('страница не показала, что досталось модели');
  if (!/схема ≈ \d+ ток/.test(итогВключения)) ошибки.push('страница не показала цену схемы');

  // 2. Вопрос, на который без трекера не ответить.
  await выполнить(`(() => {
    document.getElementById('ввод').value = 'Посмотри в трекере: какие задачи переноса сейчас в работе?';
    document.getElementById('отправить').click(); return 'ок'; })()`);
  console.log('ответ с вызовами получен за', await ждатьЧат('↳ tracker__', 180), 'с');
  const вызовы = await выполнить(`Array.from(document.querySelectorAll('#чат .вызов'))
    .map(у => у.innerText.slice(0, 120))`);
  console.log('вызовы на странице:');
  for (const в of вызовы || []) console.log('   ', в);
  if (!(вызовы || []).length) ошибки.push('строк вызовов на странице нет');
  const сбойные = await выполнить(`document.querySelectorAll('#чат .вызов.сбой').length`);
  if (сбойные) ошибки.push(`вызовов с ошибкой: ${сбойные}`);
  const ответСДанными = await выполнить(`(() => {
    const у = Array.from(document.querySelectorAll('#чат .от-агента')).pop();
    return у ? у.innerText : ''; })()`);
  console.log('в ответе есть ключи задач:', /MIG-\d/.test(ответСДанными || '') ? 'да' : 'НЕТ');
  if (!/MIG-\d/.test(ответСДанными || '')) ошибки.push('в ответе нет данных из трекера');

  // 3. Просьба изменить данные: вызова быть не должно, должна появиться заявка.
  await выполнить(`(() => {
    document.getElementById('ввод').value =
      'Добавь к задаче MIG-7 в трекере комментарий: «Проверено из браузера».';
    document.getElementById('отправить').click(); return 'ок'; })()`);
  console.log('заявка появилась за', await ждатьУзел('#заявки-панель .заявка', 180), 'с');
  const заявка = await выполнить(`(() => {
    const к = document.querySelector('#заявки-панель .заявка');
    return к ? к.innerText.replace(/\\s+/g, ' ').slice(0, 200) : ''; })()`);
  console.log('заявка:', заявка);
  if (!заявка.includes('add_comment')) ошибки.push('в заявке не тот инструмент');
  if (!заявка.includes('MIG-7')) ошибки.push('в заявке не видно аргументов');
  const самовольные = await выполнить(
    `document.getElementById('чат').innerText.includes('↳ tracker__add_comment')`);
  if (самовольные) ошибки.push('меняющий вызов выполнился без подтверждения');

  // 4. Подтверждаем — вызов происходит, результат виден, заявка исчезает.
  await выполнить(`document.querySelector('#заявки-панель .заявка-да').click(); 'ок'`);
  console.log('вызов исполнен за', await ждатьЧат('Заявка №', 120), 'с');
  const результат = await выполнить(`(() => {
    const у = document.querySelector('#чат .ответ-вызова');
    return у ? у.innerText.replace(/\\s+/g, ' ').slice(0, 160) : ''; })()`);
  console.log('ответ инструмента:', результат);
  if (!результат.includes('добавлен')) ошибки.push('результат подтверждённого вызова не показан');
  const осталось = await выполнить(`document.querySelectorAll('#заявки-панель .заявка').length`);
  console.log('заявок осталось:', осталось);
  if (осталось) ошибки.push('заявка не исчезла после исполнения');

  // 5. Кнопка «Вызвать» у инструмента в панели MCP. prompt() в headless-браузере
  // сам по себе не отвечает, поэтому подменяем его — человек в этом месте просто
  // вводит аргументы руками.
  await выполнить(`window.prompt = () => '{"key": "MIG-2"}'; 'ок'`);
  await выполнить(`document.querySelector('.mcp-подключить[data-mcp="tracker"]').click(); 'ок'`);
  await new Promise(р => setTimeout(р, 6000));
  const естьКнопка = await выполнить(`(() => {
    const к = document.querySelector('.mcp-вызвать[data-вызвать="tracker__get_issue"]');
    if (!к) return false; к.click(); return true; })()`);
  if (!естьКнопка) ошибки.push('в панели MCP нет кнопки «Вызвать»');
  else {
    console.log('ручной вызов выполнен за', await ждатьЧат('↳ tracker__get_issue', 60), 'с');
    const вручную = await выполнить(`(() => {
      const у = Array.from(document.querySelectorAll('#чат .ответ-вызова')).pop();
      return у ? у.innerText.replace(/\\s+/g, ' ').slice(0, 120) : ''; })()`);
    console.log('ответ на ручной вызов:', вручную);
    if (!вручную.includes('MIG-2')) ошибки.push('ручной вызов не вернул задачу');
  }

  console.log('\n=== итог ===');
  console.log('сообщений об ошибке в чате:',
              await выполнить(`document.querySelectorAll('#чат .ошибка').length`));
  for (const строка of (await чат()).slice(-6)) console.log('  ', строка);
  console.log(ошибки.length ? 'ОШИБКИ:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}


// --- День 21: страница индекса документов --------------------------------------
// Два режима на одной странице /rag. «индекс» — проверка для чек-листа: всё,
// что страница умеет, нажимается по разу и сверяется с ожиданием, расхождение
// валит прогон. «показ» — то же для записи экрана: медленно, с подписями внизу,
// и в конце агент сам ищет в индексе через MCP-сервер docs.
const наСтраницеИндекса = async (путь = '/rag') => {
  // Пустой путь — переход уже сделан кликом по ссылке, остаётся дождаться страницы.
  if (путь) await выполнить(`(() => { location.href = ${JSON.stringify(путь)}; return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < 30000) {
    await new Promise(р => setTimeout(р, 700));
    const готово = await выполнить(
      `!!document.getElementById('собрать') && document.getElementById('состояние').innerText !== 'Загрузка…'`);
    if (готово) return true;
  }
  ошибки.push('страница индекса не загрузилась');
  return false;
};
// Ждать надо конкретный текст внутри конкретного узла: «готово» уже написано
// от прошлой работы, и общий признак проскакивал бы мимо.
const ждатьТекст = async (ид, признак, секунд) => {
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    const текст = await выполнить(
      `(document.getElementById(${JSON.stringify(ид)}) || {}).innerText || ''`);
    if ((текст || '').includes(признак)) return (Date.now() - начало) / 1000;
    await new Promise(р => setTimeout(р, 1000));
  }
  return -1;
};
const работаИндекса = async (кнопка, секунд) => {
  // Номер работы берём до нажатия: ждём, что он сменится и новая дойдёт до конца.
  const прежняя = await выполнить(`fetch('/api/rag').then(о => о.json()).then(с => с.работа ? с.работа.id : '')`);
  await выполнить(`(() => { document.getElementById(${JSON.stringify(кнопка)}).click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1500));
    const р = await выполнить(`fetch('/api/rag').then(о => о.json()).then(с => с.работа)`);
    if (р && р.id !== прежняя && р.готово) return р;
  }
  return null;
};
const поискНаСтранице = async (запрос, где = 'обе', сколько = 3) => {
  await выполнить(`(() => {
    document.getElementById('запрос').value = ${JSON.stringify(запрос)};
    document.getElementById('где').value = ${JSON.stringify(где)};
    document.getElementById('сколько').value = ${сколько};
    return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 600));
  await выполнить(`(() => { document.getElementById('искать').click(); return 'ок'; })()`);
  await ждатьТекст('находки', '·', 60);
  await new Promise(р => setTimeout(р, 800));
  return выполнить(`(() => {
    const итог = {};
    document.querySelectorAll('#находки [data-стратегия]').forEach(к => {
      итог[к.dataset['стратегия']] = Array.from(к.querySelectorAll('.находка')).map(н => н.innerText.replace(/\\s+/g, ' '));
    });
    return итог; })()`);
};
// Чанк открывается по тексту находки, а не по месту в выдаче: место зависит
// от запроса, и однажды первым оказался чанк с контактными данными из
// приложения договора — в записи для отчёта ему не место.
const открытьЧанк = async (стратегия, место = 0, признак = '') => {
  const ид = await выполнить(`(() => {
    const к = document.querySelector('#находки [data-стратегия="${стратегия}"]');
    if (!к) return '';
    const находки = Array.from(к.querySelectorAll('.находка'));
    // Карточка показывает начало чанка (600 символов): у широкого чанка
    // признак может лежать дальше, и тогда берётся находка по месту.
    const нужная = (${JSON.stringify(признак)}
      && находки.find(н => н.innerText.includes(${JSON.stringify(признак)}))) || находки[${место}];
    const и = нужная && нужная.querySelector('.чанк-ид');
    if (!и) return '';
    и.click(); return и.dataset['ид']; })()`);
  if (ид) await ждатьТекст('чанк', ид, 20);
  return ид;
};

if (РЕЖИМ === 'индекс') {
  await наСтраницеИндекса();
  const состояние = await выполнить(`document.getElementById('состояние').innerText.replace(/\\s+/g, ' ')`);
  console.log('состояние:', состояние);
  if (!состояние.includes('эмбеддер')) ошибки.push('нет плашки эмбеддера');
  if (!/«структура» — чанков/.test(состояние)) ошибки.push('индекс «структура» не показан (соберите индекс до прогона)');
  const документы = await выполнить(`document.getElementById('документы').innerText`);
  const строк = await выполнить(`document.querySelectorAll('#документы tr').length - 1`);
  console.log('документов в таблице:', строк);
  if (строк < 1) ошибки.push('таблица документов пуста');
  if (!документы.includes('pdf')) ошибки.push('в таблице нет PDF');

  const находки = await поискНаСтранице('до какого числа платить за связь по договору VPN');
  console.log('поиск:', Object.fromEntries(Object.entries(находки).map(([к, в]) => [к, в.map(т => т.slice(0, 110))])));
  if (!находки['структура'] || !находки['фикс']) ошибки.push('поиск не показал обе стратегии рядом');
  if (!(находки['структура'] || []).some(т => т.includes('20 числа'))) ошибки.push('«структура» не нашла пункт о 20 числе');
  if (!(находки['структура'] || []).every(т => /стр\. \d/.test(т) && /(struct|fixed)-/.test(т))) {
    ошибки.push('у находки нет страницы или chunk_id');
  }

  const ид = await открытьЧанк('фикс');
  const чанк = await выполнить(`document.getElementById('чанк').innerText`);
  console.log('чанк:', ид, '→', (чанк || '').slice(0, 160).replace(/\n/g, ' '));
  if (!ид || !(чанк || '').includes(ид)) ошибки.push('чанк по chunk_id не открылся');
  if (!/предыдущего|следующего/.test(чанк || '')) ошибки.push('границы с соседями не показаны');

  const одна = await поискНаСтранице('антивирус', 'фикс', 2);
  if (Object.keys(одна).join() !== 'фикс') ошибки.push('выбор «только фикс» не сработал');

  const сравнение = await работаИндекса('сравнить', 300);
  console.log('сравнение:', сравнение && (сравнение.ошибка || JSON.stringify(сравнение.итог)));
  if (!сравнение || сравнение.ошибка) ошибки.push('сравнение стратегий не отработало');
  await ждатьТекст('сравнение', 'Как находится', 20);
  const таблица = await выполнить(`document.getElementById('сравнение').innerText.replace(/\\s+/g, ' ')`);
  for (const слово of ['Как нарезано', 'hit@1', 'mrr', 'По вопросам']) {
    if (!таблица.includes(слово)) ошибки.push(`в сравнении нет «${слово}»`);
  }
  // Ошибка ввода — сообщением на странице, а не молчанием. Сначала ждём, пока
  // страница сама увидит конец сравнения и включит кнопку: водитель узнаёт о
  // нём из API раньше страницы, и щелчок по ещё выключенной кнопке пропадал.
  for (let i = 0; i < 30; i++) {
    if (await выполнить(`!document.getElementById('собрать').disabled`)) break;
    await new Promise(р => setTimeout(р, 500));
  }
  await выполнить(`(() => { document.getElementById('размер').value = 50;
    document.getElementById('перекрытие').value = 60;
    document.getElementById('собрать').click(); return 'ок'; })()`);
  if (await ждатьТекст('работа', 'Перекрытие должно быть меньше', 10) < 0) {
    ошибки.push('неверные параметры сборки не объяснены');
  }
  console.log(ошибки.length ? 'ОШИБКИ:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 22: RAG-запрос с документами и без ------------------------------------
// Карточки «Вопрос по документам» и «Контрольные вопросы» на странице /rag.
// Помощники Дня 21 (наСтраницеИндекса, ждатьТекст) объявлены выше и годятся здесь.
const спроситьДокументы = async (вопрос, режим = 'оба', секунд = 120) => {
  await выполнить(`(() => {
    document.getElementById('rag-вопрос').value = ${JSON.stringify(вопрос)};
    document.getElementById('rag-режим').value = ${JSON.stringify(режим)};
    const м = document.getElementById('rag-модель');
    if ([...м.options].some(о => о.value === ${JSON.stringify(МОДЕЛЬ)})) м.value = ${JSON.stringify(МОДЕЛЬ)};
    document.getElementById('rag-ответы').innerHTML = '';
    return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 900));
  await выполнить(`(() => { document.getElementById('rag-спросить').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1000));
    const готово = await выполнить(`document.querySelectorAll('#rag-ответы .ответ').length > 0
      || !!document.querySelector('#rag-ответы .ошибка')`);
    if (готово) break;
  }
  return выполнить(`(() => {
    const итог = {};
    document.querySelectorAll('#rag-ответы .ответ').forEach(о => {
      итог[о.dataset['режим']] = о.innerText.replace(/\\s+/g, ' '); });
    const о = document.querySelector('#rag-ответы .ошибка');
    if (о) итог['ошибка'] = о.innerText;
    return итог; })()`);
};
const прогнатьКонтроль = async (секунд = 600) => {
  const прежний = await выполнить(`fetch('/api/rag').then(о => о.json()).then(с => с.контроль ? с.контроль.when : '')`);
  await выполнить(`(() => { document.getElementById('прогнать-контроль').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 2000));
    const с = await выполнить(`fetch('/api/rag').then(о => о.json()).then(с => ({
      работа: с.работа, когда: с.контроль ? с.контроль.when : '', итог: с.контроль ? с.контроль.totals : null}))`);
    if (с.работа && с.работа.готово && с.работа.вид === 'контроль' && (с.когда !== прежний || с.работа.ошибка)) return с;
  }
  return null;
};

if (РЕЖИМ === 'rag') {
  await наСтраницеИндекса();
  const заголовок = await выполнить('document.title');
  // С Дня 23 страница называется «RAG: реранкинг…»; карточки Дня 22 те же.
  if (!заголовок.startsWith('RAG')) ошибки.push('заголовок /rag не RAG: ' + заголовок);
  const оба = await спроситьДокументы('Какая неустойка за просрочку оплаты предусмотрена договором с ВымпелКомом?');
  console.log('без RAG:', (оба['без'] || '').slice(0, 220));
  console.log('с RAG:', (оба['rag'] || '').slice(0, 320));
  if (!оба['без'] || !оба['rag']) ошибки.push('режим «оба» не показал два ответа рядом');
  if (!/0,2\s*%|0\.2\s*%/.test(оба['rag'] || '')) ошибки.push('с RAG нет «0,2 %»');
  if (!/Источники ответа: \[1\]|\[\d\] Вымпелком/.test(оба['rag'] || '')) ошибки.push('с RAG не показан источник');
  if (!/Фрагменты в промпте: \d/.test(оба['rag'] || '')) ошибки.push('не показаны фрагменты промпта');
  if (!/без документов/.test(оба['без'] || '')) ошибки.push('без RAG нет пометки об отсутствии источников');
  const выделено = await выполнить(`document.querySelectorAll('#rag-ответы .ссылка-n').length`);
  if (!выделено) ошибки.push('ссылки [n] в тексте не подсвечены');
  const ловушка = await спроситьДокументы('Сколько стоит подписка на Microsoft Office 365 в бюджете ИТ на 2026 год?', 'rag');
  console.log('ловушка с RAG:', (ловушка['rag'] || '').slice(0, 200));
  // С Дня 24 фраза идёт после «Не знаю:» — со строчной буквы.
  if (!/в документах этого нет/i.test(ловушка['rag'] || '')) ошибки.push('ловушка с RAG не ответила «в документах этого нет»');
  if (ловушка['без']) ошибки.push('режим «только с RAG» показал и ответ без RAG');
  const контроль = await прогнатьКонтроль();
  // Кнопка с Дня 23 прогоняет пять режимов; «база» — это поиск Дня 22.
  console.log('контроль:', контроль && JSON.stringify(контроль.итог && {без: контроль.итог['без'].верно_по_правилам, база: контроль.итог['база'].верно_по_правилам}));
  if (!контроль || контроль.работа.ошибка) ошибки.push('контрольные вопросы не прогнались');
  await ждатьТекст('контроль', 'По вопросам', 20);
  const таблица = await выполнить(`document.getElementById('контроль').innerText.replace(/\\s+/g, ' ')`);
  for (const слово of ['верно по правилам', 'ловушек пройдено', 'судья: верно', 'c09', 'ловушка']) {
    if (!таблица.includes(слово)) ошибки.push(`в таблице контроля нет «${слово}»`);
  }
  console.log(ошибки.length ? 'ОШИБКИ:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 23: второй этап поиска — реранкинг, фильтр и rewrite -----------------
// Карточки «Поиск в два этапа» и «Подбор порога и top-K» на /rag. Помощники
// Дней 21–22 (наСтраницеИндекса, ждатьТекст, прогнатьКонтроль) объявлены выше.
const показатьОтборНаСтранице = async (вопрос, режим = 'полный', поправки = {}, секунд = 300) => {
  await выполнить(`(() => {
    const п = ${JSON.stringify(поправки)};
    document.getElementById('отбор-вопрос').value = ${JSON.stringify(вопрос)};
    document.getElementById('отбор-режим').value = ${JSON.stringify(режим)};
    document.getElementById('отбор-реранкер').value = п.реранкер ?? '*';
    document.getElementById('отбор-kдо').value = п.kдо ?? '';
    document.getElementById('отбор-kпосле').value = п.kпосле ?? '';
    document.getElementById('отбор-порог').value = п.порог ?? '';
    document.getElementById('отбор-rewrite').value = п.rewrite ?? '*';
    document.getElementById('отбор').innerHTML = '';
    return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 900));
  await выполнить(`(() => { document.getElementById('показать-отбор').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1000));
    const готово = await выполнить(`!!document.querySelector('#отбор table') || !!document.querySelector('#отбор .ошибка')`);
    if (готово) break;
  }
  // Строка кандидата — судьба, место «было» и текст: по «было» видно, откуда
  // реранкер поднял фрагмент.
  return выполнить(`(() => ({
    текст: document.getElementById('отбор').innerText.replace(/\\s+/g, ' '),
    строки: Array.from(document.querySelectorAll('#отбор tr[data-судьба]')).map(т => ({
      судьба: т.dataset['судьба'], было: +т.children[1].innerText,
      текст: т.innerText.replace(/\\s+/g, ' ')})),
    ошибка: (document.querySelector('#отбор .ошибка') || {}).innerText || ''
  }))()`);
};
const вПромпт = (о) => (о.строки || []).filter(с => с.судьба === 'в промпт');
const ответитьВсеНаСтранице = async (вопрос, секунд = 400) => {
  await выполнить(`(() => {
    document.getElementById('отбор-вопрос').value = ${JSON.stringify(вопрос)};
    const м = document.getElementById('rag-модель');
    if ([...м.options].some(о => о.value === ${JSON.stringify(МОДЕЛЬ)})) м.value = ${JSON.stringify(МОДЕЛЬ)};
    document.getElementById('ответы-поиск').innerHTML = '';
    return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 700));
  await выполнить(`(() => { document.getElementById('ответить-все').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1000));
    const готово = await выполнить(`document.querySelectorAll('#ответы-поиск [data-поиск]').length > 0
      || !!document.querySelector('#ответы-поиск .ошибка')`);
    if (готово) break;
  }
  return выполнить(`(() => {
    const итог = {};
    document.querySelectorAll('#ответы-поиск [data-поиск]').forEach(о => {
      итог[о.dataset['поиск']] = о.innerText.replace(/\\s+/g, ' '); });
    const о = document.querySelector('#ответы-поиск .ошибка');
    if (о) итог['ошибка'] = о.innerText;
    return итог; })()`);
};
// Вопросы показа и проверки. c03 — промах Дня 22: пункт 2.2.7 о тарифах стоит
// в длинном разделе, и косинус ставил его десятым. Ловушка — из стенда подбора.
const ВОПРОС_ТАРИФЫ = 'За сколько дней Ростелеком обязан предупредить об изменении тарифов по договору VPN?';
const ВОПРОС_ЛОВУШКА = 'Сколько точек доступа Wi-Fi закупается по бюджету ИТ на 2026 год?';
// Пункт 2.2.7 стоит в конце чанка «2. Права и обязанности сторон» договора VPN;
// в строке отбора виден раздел, а не номер пункта внутри чанка.
const этоТарифы = (с) => /Ростелеком VPN/.test(с.текст) && /Права и обязанности/.test(с.текст);

if (РЕЖИМ === 'реранк') {
  await наСтраницеИндекса();
  const заголовок = await выполнить('document.title');
  // С Дня 24 страница называется «RAG: цитаты…»; карточки Дня 23 те же.
  if (!заголовок.startsWith('RAG')) ошибки.push('заголовок /rag не RAG: ' + заголовок);
  const режимы = await выполнить(`Array.from(document.querySelectorAll('#режимы-поиска tr[data-режим-поиска]')).map(т => т.getAttribute('data-режим-поиска'))`);
  console.log('режимы поиска:', режимы.join(', '));
  if (режимы.join() !== 'база,фильтр,rewrite,полный') ошибки.push('таблица режимов не та: ' + режимы.join());
  const источник = await выполнить(`document.getElementById('настройки-источник').innerText`);
  console.log('параметры:', источник);
  if (!источник.includes('rag-settings.json')) ошибки.push('режимы не из rag-settings.json (подбора не было?)');
  if (await ждатьТекст('подбор', 'Выбрано: реранкер', 20) < 0) ошибки.push('карточка подбора не показала выбранное');

  // База Дня 22: нужный пункт не в промпте. Фильтр: реранкер поднимает его.
  const база = await показатьОтборНаСтранице(ВОПРОС_ТАРИФЫ, 'база');
  console.log('база, в промпт:', вПромпт(база).map(с => с.текст.slice(0, 90)));
  if (база.ошибка) ошибки.push('отбор «база»: ' + база.ошибка);
  if (/реранкер «/.test(база.текст)) ошибки.push('в режиме «база» есть этап реранкера');
  const фильтр = await показатьОтборНаСтранице(ВОПРОС_ТАРИФЫ, 'фильтр');
  console.log('фильтр:', фильтр.текст.slice(0, 400));
  console.log('фильтр, в промпт:', вПромпт(фильтр).map(с => `было ${с.было}: ` + с.текст.slice(0, 90)));
  if (фильтр.ошибка) ошибки.push('отбор «фильтр»: ' + фильтр.ошибка);
  for (const слово of ['2) первый этап', '3) порог косинуса', '4) реранкер «', '5) фильтр']) {
    if (!фильтр.текст.includes(слово)) ошибки.push(`в отборе «фильтр» нет этапа «${слово}»`);
  }
  if (!вПромпт(фильтр).some(с => с.было > 5)) ошибки.push('реранкер не поднял ни одного кандидата из-за пределов top-5');
  if (!вПромпт(фильтр).some(этоТарифы)) ошибки.push('раздел 2 договора VPN (п. 2.2.7) не попал в промпт после фильтра');

  const полный = await показатьОтборНаСтранице(ВОПРОС_ТАРИФЫ, 'полный');
  console.log('полный:', полный.текст.slice(0, 400));
  if (!/1\) запросы/.test(полный.текст) || !полный.текст.includes('исходный:')) ошибки.push('в режиме «полный» не показаны переписанные запросы');

  // Порог и K вживую. Подобранный порог отсёк часть кандидатов (строки «ниже
  // порога» у фильтра выше), порог 0 пропускает всех до K после. «Порог 0,999
  // режет всё» для LLM-реранкера не годится: прямому ответу он ставит 10 из 10.
  if (!фильтр.строки.some(с => с.судьба === 'ниже порога')) ошибки.push('подобранный порог никого не отсёк');
  const порог = await показатьОтборНаСтранице(ВОПРОС_ТАРИФЫ, 'фильтр', {порог: '0'});
  console.log('порог 0: в промпт', вПромпт(порог).length, '· ниже порога', порог.строки.filter(с => с.судьба === 'ниже порога').length,
              '; подобранный: в промпт', вПромпт(фильтр).length);
  if (порог.строки.some(с => с.судьба === 'ниже порога')) ошибки.push('порог 0 кого-то отсёк');
  if (вПромпт(порог).length <= вПромпт(фильтр).length) ошибки.push('без порога в промпт ушло не больше, чем с порогом');
  const k2 = await показатьОтборНаСтранице(ВОПРОС_ТАРИФЫ, 'фильтр', {kпосле: '2', порог: '0'});
  console.log('K после 2: в промпт', вПромпт(k2).length);
  if (вПромпт(k2).length !== 2) ошибки.push('K после = 2 не оставил ровно два фрагмента');
  if (!k2.строки.some(с => с.судьба === 'за пределами K')) ошибки.push('нет строк «за пределами K»');
  const ловушка = await показатьОтборНаСтранице(ВОПРОС_ЛОВУШКА, 'фильтр');
  console.log('ловушка, фильтр: в промпт', вПромпт(ловушка).length, 'из', ловушка.строки.length);

  const ответы = await ответитьВсеНаСтранице(ВОПРОС_ТАРИФЫ);
  for (const [м, т] of Object.entries(ответы)) console.log(`ответ «${м}»:`, т.slice(0, 200));
  if (Object.keys(ответы).join() !== 'база,фильтр,rewrite,полный') ошибки.push('не четыре ответа: ' + Object.keys(ответы).join());
  if (!/\b10\b/.test(ответы['фильтр'] || '')) ошибки.push('с фильтром ответ не назвал 10 дней');

  const контроль = await прогнатьКонтроль(2400);
  const т = контроль && контроль.итог;
  if (т) console.log('верно по правилам:', Object.entries(т).map(([м, x]) => `${м} ${x.верно_по_правилам}`).join(', '));
  if (!контроль || контроль.работа.ошибка) ошибки.push('контрольные вопросы не прогнались');
  await ждатьТекст('контроль', 'По вопросам', 20);
  const таблица = await выполнить(`document.getElementById('контроль').innerText.replace(/\\s+/g, ' ')`);
  for (const слово of ['без RAG', '+ фильтр', '+ rewrite', '+ rewrite и фильтр', 'фрагментов в промпте', 'время поиска']) {
    if (!таблица.includes(слово)) ошибки.push(`в таблице контроля нет «${слово}»`);
  }
  console.log(ошибки.length ? 'ОШИБКИ:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 24: цитаты, источники и «не знаю» -------------------------------------
// Карточки «Ответ с источниками и цитатами», «Проверка цитат» и «Порог «не
// знаю»» на /rag. Помощники Дней 21–23 (наСтраницеИндекса, ждатьТекст,
// работаИндекса) объявлены выше.
const спроситьСЦитатами = async (вопрос, поправки = {}, секунд = 240) => {
  await выполнить(`(() => {
    const п = ${JSON.stringify(поправки)};
    document.getElementById('цит-вопрос').value = ${JSON.stringify(вопрос)};
    document.getElementById('цит-поиск').value = п.поиск ?? 'фильтр';
    document.getElementById('цит-формат').value = п.формат ?? 'цитаты';
    document.getElementById('цит-порог').value = п.порог ?? '';
    const м = document.getElementById('rag-модель');
    if ([...м.options].some(о => о.value === ${JSON.stringify(МОДЕЛЬ)})) м.value = ${JSON.stringify(МОДЕЛЬ)};
    document.getElementById('цит-ответ').innerHTML = '';
    return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 800));
  await выполнить(`(() => { document.getElementById('цит-спросить').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1000));
    const готово = await выполнить(`!!document.querySelector('#цит-ответ .ответ') || !!document.querySelector('#цит-ответ > .ошибка')`);
    if (готово) break;
  }
  return выполнить(`(() => ({
    текст: (document.getElementById('цит-ответ').innerText || '').replace(/\\s+/g, ' '),
    неЗнаю: !!document.querySelector('#цит-ответ [data-не-знаю]'),
    цитат: document.querySelectorAll('#цит-ответ .цитата').length,
    проверки: Array.from(document.querySelectorAll('#цит-ответ .цитата')).map(ц => ц.dataset['проверка']),
    источников: document.querySelectorAll('#цит-ответ .источник.процитирован').length,
    подсвечено: document.querySelectorAll('#цит-ответ mark.цит').length,
    ошибка: (document.querySelector('#цит-ответ > .ошибка') || {}).innerText || ''
  }))()`);
};
const ПОДТВЕРЖДЁННЫЕ = ['дословно', 'почти', 'в другом фрагменте'];
const проверитьНаВопросах = async (секунд = 1200) => {
  const прежний = await выполнить(`fetch('/api/rag').then(о => о.json()).then(с => с.цитаты ? с.цитаты.when : '')`);
  await выполнить(`(() => { document.getElementById('проверить-цитаты').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 2000));
    const с = await выполнить(`fetch('/api/rag').then(о => о.json()).then(с => ({
      работа: с.работа, когда: с.цитаты ? с.цитаты.when : '', итог: с.цитаты ? с.цитаты.totals : null}))`);
    if (с.работа && с.работа.готово && с.работа.вид === 'цитаты' && (с.когда !== прежний || с.работа.ошибка)) return с;
  }
  return null;
};
// Вопросы показа и проверки — из контрольных Дня 22 и расплывчатых Дня 24.
const ВОПРОС_НЕУСТОЙКА = 'Какая неустойка за просрочку оплаты предусмотрена договором с ВымпелКомом?';
const ВОПРОС_WIFI = 'Сколько точек доступа Wi-Fi закупается по бюджету ИТ на 2026 год?';
const ВОПРОС_ЭТО = 'А сколько это стоит?';
const ВОПРОС_ДАЛЬШЕ = 'Что с ним делать дальше?';

if (РЕЖИМ === 'цитаты') {
  await наСтраницеИндекса();
  const заголовок = await выполнить('document.title');
  // С Дня 28 страница называется по неделе 6; карточки цитат Дня 24 — на ней же.
  if (!заголовок.startsWith('RAG: локальный конвейер')) ошибки.push('заголовок /rag не тот: ' + заголовок);
  const порог = await выполнить(`document.getElementById('цит-порог-сейчас').innerText`);
  console.log('порог:', порог);
  if (!/порог «не знаю»: 0\.\d/.test(порог)) ошибки.push('не показан порог «не знаю»');

  const ответ = await спроситьСЦитатами(ВОПРОС_НЕУСТОЙКА);
  console.log('ответ:', ответ.текст.slice(0, 400));
  console.log('цитаты:', ответ.проверки, '· источников', ответ.источников, '· подсвечено', ответ.подсвечено);
  if (ответ.ошибка) ошибки.push('ответ с цитатами: ' + ответ.ошибка);
  if (ответ.неЗнаю) ошибки.push('на вопрос с ответом — «не знаю»');
  if (!/0[,.]2\s*%/.test(ответ.текст)) ошибки.push('в ответе нет «0,2 %»');
  if (!ответ.источников || !/struct-/.test(ответ.текст)) ошибки.push('нет источника с chunk_id');
  if (!ответ.цитат || !ответ.проверки.every(п => ПОДТВЕРЖДЁННЫЕ.includes(п))) ошибки.push('цитаты не подтверждены кодом');
  if (!ответ.подсвечено) ошибки.push('цитата не подсвечена во фрагменте');

  const ловушка = await спроситьСЦитатами(ВОПРОС_WIFI);
  console.log('ловушка:', ловушка.текст.slice(0, 300));
  if (!ловушка.неЗнаю || !/порог уверенности \(код\)/.test(ловушка.текст) || !/Уточняющий вопрос: .+\?/.test(ловушка.текст)) {
    ошибки.push('ловушка: нет «не знаю» по порогу с уточнением');
  }
  if (ловушка.цитат || ловушка.источников) ошибки.push('у «не знаю» есть источники или цитаты');
  const неясно = await спроситьСЦитатами(ВОПРОС_ЭТО);
  console.log('неоднозначный:', неясно.текст.slice(0, 300));
  if (!неясно.неЗнаю || !/Уточняющий вопрос/.test(неясно.текст)) ошибки.push('неоднозначный вопрос: нет «не знаю» с уточнением');
  // Поправка порога: у вопроса о Консультант Плюс лучшая оценка реранкера 0,8 —
  // при пороге 0,9 ответа по документам нет. «Не знаю» — строго ниже порога,
  // поэтому вопрос с оценкой 1,0 для такой проверки не годится.
  const строгий = await спроситьСЦитатами('Во сколько в год обходится информационное обслуживание Консультант Плюс?', {порог: '0.9'});
  console.log('порог 0.9:', строгий.текст.slice(0, 200));
  if (!строгий.неЗнаю || !/при пороге 0.90/.test(строгий.текст)) ошибки.push('порог 0,9 не перевёл ответ в «не знаю»');
  const текстом = await спроситьСЦитатами(ВОПРОС_НЕУСТОЙКА, {формат: 'текст'});
  console.log('формат Дня 23:', текстом.текст.slice(0, 200));
  if (текстом.цитат || !/цитат нет — формат Дня 23/.test(текстом.текст)) ошибки.push('формат Дня 23 показал цитаты');

  const проверка = await проверитьНаВопросах();
  const т = проверка && проверка.итог && проверка.итог['цитаты'];
  if (т) console.log('проверка:', JSON.stringify({источники: `${т.с_источниками}/${т.отвечено}`, цитаты: `${т.с_цитатами}/${т.отвечено}`,
    смысл_код: т.смысл_код, судья: `${т.смысл_да}/${т.смысл_частично}/${т.смысл_нет}`, ловушки: т.ловушки_не_знаю, расплывчатые: т.расплывчатые_не_знаю}));
  if (!проверка || проверка.работа.ошибка) ошибки.push('проверка цитат не прошла');
  else {
    if (т.с_источниками !== т.отвечено || т.с_цитатами !== т.отвечено) ошибки.push('не в каждом ответе источники и цитаты');
    if (т.ловушки_не_знаю !== т.ловушек) ошибки.push('не все ловушки — «не знаю» с уточнением');
  }
  await ждатьТекст('проверка', 'По вопросам', 20);
  const таблица = await выполнить(`document.getElementById('проверка').innerText.replace(/\\s+/g, ' ')`);
  for (const слово of ['источники в каждом ответе', 'цитаты в каждом ответе', 'смысл совпадает с цитатами — код',
                       'расплывчатые: «не знаю» с уточнением', 'с цитатами (День 24)', 'без цитат (День 23)', 'v1']) {
    if (!таблица.includes(слово)) ошибки.push(`в таблице проверки нет «${слово}»`);
  }
  const подбор = await работаИндекса('подобрать-уверенность', 300);
  console.log('подбор порога:', подбор && (подбор.ошибка || JSON.stringify(подбор.итог)));
  if (!подбор || подбор.ошибка) ошибки.push('подбор порога «не знаю» не прошёл');
  if (await ждатьТекст('уверенность', 'Действующий порог', 20) < 0) ошибки.push('карточка порога пуста');
  console.log(ошибки.length ? 'ОШИБКИ:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 25: мини-чат с RAG и памятью задачи ------------------------------------

const наСтраницеЧата = async (путь = '/chat') => {
  const перейти = () => выполнить(`(() => { location.href = ${JSON.stringify(путь)}; return 'ок'; })()`);
  if (путь) await перейти();
  const начало = Date.now();
  let переход = Date.now();
  while (Date.now() - начало < 60000) {
    await new Promise(р => setTimeout(р, 700));
    // Переход потерялся (страница ещё грузила прежний адрес) — повторить.
    if (путь && Date.now() - переход > 10000 && await выполнить(`location.pathname`) !== путь) {
      await перейти();
      переход = Date.now();
    }
    const готово = await выполнить(
      `!!document.getElementById('чат-отправить') && (document.getElementById('память-задачи') || {}).innerText !== ''`);
    if (готово) return true;
  }
  ошибки.push('страница чата не загрузилась');
  return false;
};
// Последний ответ ассистента в ленте — тем, что видит человек.
const последнийОтвет = () => выполнить(`(() => {
  const все = document.querySelectorAll('#чат-лента .сообщение.ассистент');
  const о = все[все.length - 1];
  if (!о) return null;
  const документы = о.querySelectorAll('.источник.документ');
  return {сообщений: все.length, номер: Number(о.dataset.номер), вид: о.dataset.вид,
          неЗнаю: о.getAttribute('data-не-знаю') === '1',
          источников: Number((о.querySelector('.источники') || {dataset: {}}).dataset.источников || 0),
          документов: документы.length,
          сChunkId: [...документы].filter(д => д.querySelector('code')).length,
          цитат: о.querySelectorAll('.источник.документ .цитата').length,
          память: [...о.querySelectorAll('.источник.память .код')].map(к => к.innerText.trim()),
          памятьСДокументами: [...о.querySelectorAll('.источник.память')].filter(п => п.querySelector('.цитата')).length,
          проверено: о.querySelectorAll('.источник.проверено').length,
          понято: (о.querySelector('.понято') || {}).innerText || '',
          цель: (о.querySelector('.строка-цели') || {}).innerText || '',
          источникиТекст: (о.querySelector('.источники') || {}).innerText || '',
          текст: (о.querySelector('.текст') || {}).innerText || ''};
})()`);
const сказатьВЧате = async (текст, секунд = 240) => {
  const было = (await последнийОтвет() || {сообщений: 0}).сообщений;
  await выполнить(`(() => {
    const поле = document.getElementById('чат-реплика');
    поле.value = ${JSON.stringify(текст)};
    document.getElementById('чат-отправить').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1000));
    const о = await последнийОтвет();
    if (о && о.сообщений > было) return о;
    const сбой = await выполнить(`(document.querySelector('#чат-ход .ошибка') || {}).innerText || ''`);
    if (сбой) { ошибки.push('чат: ' + сбой); return {ошибка: сбой}; }
  }
  ошибки.push('чат не ответил: ' + текст.slice(0, 60));
  return {ошибка: 'нет ответа'};
};
const памятьНаСтранице = (ид) => выполнить(`(document.getElementById(${JSON.stringify(ид)}) || {}).innerText || ''`);
const открытьСессию = async (имя) => {
  await выполнить(`(() => { document.getElementById('чат-сессия').value = ${JSON.stringify(имя)};
    document.getElementById('чат-открыть').click(); return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 1500));
};
const новыйРазговорНаСтранице = async () => {
  await выполнить(`(() => { window.confirm = () => true; document.getElementById('чат-новый').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < 15000) {
    await new Promise(р => setTimeout(р, 500));
    if (!(await последнийОтвет())) return true;
  }
  return false;
};
const сценарииНаСтранице = async (выбор = '', судья = true, секунд = 1500) => {
  const прежняя = await выполнить(`fetch('/api/chat?session=x').then(о => о.json()).then(с => с.работа ? с.работа.id : '')`);
  await выполнить(`(() => { document.getElementById('сценарии-выбор').value = ${JSON.stringify(выбор)};
    document.getElementById('сценарии-судья').checked = ${судья};
    document.getElementById('сценарии-запустить').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 2000));
    const с = await выполнить(`fetch('/api/chat?session=x').then(о => о.json()).then(с => ({р: с.работа, п: с.сценарии}))`);
    if (с.р && с.р.id !== прежняя && с.р.готово) return {работа: с.р, прогон: с.п};
  }
  ошибки.push('сценарии не закончились');
  return null;
};

const S1 = [
  'Мне нужно сравнить два договора связи — Ростелеком VPN и ВымпелКом (стационарная связь): сроки действия, порядок оплаты и ответственность сторон. Начнём с Ростелекома: на какой срок заключён договор?',
  'До какого числа по нему нужно платить каждый месяц?',
  'Ограничение: суммы приводи как в документе, без пересчёта, и обязательно указывай, включён ли НДС.',
  'Какова общая стоимость услуг по нему?',
  'Термин: «срок уведомления» — это за сколько дней оператор обязан предупредить об изменении тарифов или условий договора. Какой срок уведомления у каждого из двух операторов?',
  'Кстати, сколько точек доступа Wi-Fi закупается по бюджету ИТ?',
  'Напомни цель и что мы ещё не сравнили.',
];

if (РЕЖИМ === 'чат') {
  const главная = await выполнить(`!!document.getElementById('чат-ссылка-25')`);
  if (!главная) ошибки.push('на главной нет ссылки на мини-чат');
  await наСтраницеЧата('/chat');
  const заголовок = await выполнить('document.title');
  if (!заголовок.startsWith('Мини-чат')) ошибки.push('заголовок /chat: ' + заголовок);
  await открытьСессию('проверка-браузер');
  await новыйРазговорНаСтранице();

  const о1 = await сказатьВЧате('Мне нужно сравнить сроки оплаты по двум договорам связи — Ростелеком VPN и ВымпелКом. Начнём: до какого числа нужно платить Ростелекому за VPN?');
  console.log('1:', о1.вид, '|', о1.понято.replace(/\n/g, ' / ').slice(0, 160), '|', о1.текст.slice(0, 120));
  if (о1.вид !== 'документы' || !о1.документов || о1.сChunkId !== о1.документов || !о1.цитат) ошибки.push('1: нет документа с chunk_id и цитатой');
  if (!/20 числа/.test(о1.текст)) ошибки.push('1: нет «20 числа»');
  const цель = await памятьНаСтранице('цель-текст');
  console.log('цель:', цель);
  if (!/Ростелеком/.test(цель) || !/Вымпел/i.test(цель)) ошибки.push('цель не записана в память задачи');

  const о2 = await сказатьВЧате('А у ВымпелКома сколько дней даётся на оплату счёта?');
  console.log('2:', о2.понято.replace(/\n/g, ' / ').slice(0, 160), '|', о2.текст.slice(0, 120));
  if (!/Вымпел/i.test(о2.источникиТекст) || !/30/.test(о2.текст)) ошибки.push('2: «у ВымпелКома» — не тот договор или нет 30 дней');

  const о3 = await сказатьВЧате('Ограничение: суммы приводи как в документе и обязательно указывай, включён ли НДС.');
  const ограничения = await памятьНаСтранице('память-ограничения');
  console.log('3:', о3.вид, '| ограничения:', ограничения.replace(/\n/g, ' / '));
  if (о3.вид !== 'разговор' || !о3.память.length) ошибки.push('3: ограничение не понято как реплика о разговоре');
  if (!/НДС/.test(ограничения)) ошибки.push('3: ограничение не записано');

  const о4 = await сказатьВЧате('Сколько точек доступа Wi-Fi закупается по бюджету ИТ?');
  console.log('4:', о4.неЗнаю, '| проверено', о4.проверено, '|', о4.цель.slice(0, 120));
  if (!о4.неЗнаю || !о4.проверено) ошибки.push('4: ловушка без «не знаю» и проверенных разделов');
  if (!/Ростелеком/.test(о4.цель)) ошибки.push('4: цель пропала после ловушки');

  const о5 = await сказатьВЧате('Какая у нас цель и что уже выяснили?');
  console.log('5:', о5.вид, '| коды', о5.память.join(','), '| с документами', о5.памятьСДокументами);
  if (о5.вид !== 'разговор' || !о5.память.includes('Ц') || !о5.памятьСДокументами) ошибки.push('5: ответ по памяти без цели или без «выяснено» с документами');

  const всего = (await последнийОтвет()).сообщений;
  const безИсточников = await выполнить(`[...document.querySelectorAll('#чат-лента .сообщение.ассистент .источники')].filter(и => Number(и.dataset.источников) === 0).length`);
  if (всего !== 5 || безИсточников) ошибки.push(`ответов ${всего}, без источников ${безИсточников}`);
  await наСтраницеЧата('/chat');
  await открытьСессию('проверка-браузер');
  const послеПерезагрузки = (await последнийОтвет() || {сообщений: 0}).сообщений;
  console.log('после перезагрузки страницы ответов:', послеПерезагрузки);
  if (послеПерезагрузки !== 5) ошибки.push('история не восстановилась после перезагрузки');
  await новыйРазговорНаСтранице();
  if (await последнийОтвет() || !/ещё не названа/.test(await памятьНаСтранице('цель-текст'))) ошибки.push('новый разговор не стёр историю и память');

  const итог = await сценарииНаСтранице('', true);
  if (итог && итог.прогон) {
    for (const с of итог.прогон.scenarios) {
      const т = с.totals;
      console.log(`${с.id}: ходов ${т.ходов}, с источниками ${т.с_источниками}, документ+цитата ${т.с_документом_и_цитатой}/${т.документных}, ` +
                  `факты ${т.факты}, цель на месте ${т.цель_на_месте}, провалов ${т.провалов}, судья ${с.judge.цель_удержана || '—'}`);
      if (т.с_источниками !== т.ходов) ошибки.push(`${с.id}: не во всех ответах источники`);
      if (т.провалов) ошибки.push(`${с.id}: провалов ${т.провалов}`);
    }
    if (!(await ждатьТекст('сценарии', 'цель на месте', 20) >= 0)) ошибки.push('таблица сценариев не нарисована');
  } else ошибки.push('сценарии не прогнались');
  console.log(ошибки.length ? 'ОШИБКИ:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 26: локальная модель (Ollama) -------------------------------------------

const наСтраницеЛокальной = async (путь = '/local') => {
  if (путь) await выполнить(`(() => { location.href = ${JSON.stringify(путь)}; return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < 30000) {
    await new Promise(р => setTimeout(р, 700));
    const готово = await выполнить(
      `!!document.getElementById('лок-спросить') && !!document.getElementById('модели-ollama')`);
    if (готово) return true;
  }
  ошибки.push('страница /local не загрузилась');
  return false;
};
const ответыЛокальной = () => выполнить(`[...document.querySelectorAll('#лок-ответы .ответ')].map(о => ({
  модель: о.dataset.модель, готово: о.dataset.готово, текст: (о.querySelector('.текст') || {}).innerText || '',
  итог: (о.querySelector('.итог') || {}).innerText || '', ttft: Number(о.dataset.ttft || 0),
  tokS: Number(о.dataset.tokS || 0), локально: о.classList.contains('локально')}))`);
const спроситьНаЛокальной = async (вопрос, {режим = 'both', коротко = true, секунд = 600} = {}) => {
  await выполнить(`(() => {
    document.getElementById('лок-вопрос').value = ${JSON.stringify(вопрос)};
    document.getElementById('лок-режим').value = ${JSON.stringify(режим)};
    document.getElementById('лок-коротко').checked = ${коротко};
    document.getElementById('лок-спросить').click(); return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 800));
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    const ответы = await ответыЛокальной();
    if (ответы.length && ответы.every(о => о.готово !== '0')) return ответы;
    await new Promise(р => setTimeout(р, 1000));
  }
  ошибки.push('локальная модель не ответила: ' + вопрос.slice(0, 50));
  return await ответыЛокальной();
};
const доступНаСтранице = async (вид, секунд = 600) => {
  await выполнить(`(() => { document.getElementById('доступ').innerHTML = '';
    document.getElementById(${JSON.stringify('через-' + вид)}).click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1000));
    const р = await выполнить(`(() => { const и = document.getElementById('доступ-итог');
      const о = document.querySelector('#доступ .ошибка');
      return о ? {ошибка: о.innerText} : (и ? {вид: и.dataset.вид, итог: и.innerText,
        вывод: (document.getElementById('доступ-вывод') || {}).innerText || ''} : null); })()`);
    if (р) return р;
  }
  ошибки.push('нет ответа на ' + вид);
  return {ошибка: 'нет ответа'};
};
const сырыеJSON = async (путь) => {
  const адрес = await выполнить(`(document.querySelector('a.сырой') || {}).href || ''`);
  const база = адрес ? new URL(адрес).origin : 'http://127.0.0.1:11434';
  await выполнить(`(() => { location.href = ${JSON.stringify('')} + ${JSON.stringify(база)} + ${JSON.stringify(путь)}; return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 2500));
  return await выполнить(`document.body ? document.body.innerText : ''`);
};
const ВОПРОС_ПРОСТОЙ = 'Какая столица Франции? Ответь одним словом.';
const ВОПРОС_СРЕДНИЙ = 'Договор действует с 01.01.2025 по 31.12.2027, оплата вносится один раз в месяц. Сколько всего ежемесячных платежей за весь срок договора? Ответь одним числом.';

// --- День 27: локальная модель в приложении — офлайн-режим -------------------------

const ОФЛАЙН_РЕПЛИКА = 'Мне нужно сравнить два договора связи — Ростелеком VPN и ВымпелКом: сроки, порядок оплаты и ответственность. Начнём с Ростелекома: до какого числа нужно платить каждый месяц?';
const ОФЛАЙН_MCP = 'Какой статус и приоритет у задачи MIG-8 в трекере?';
const полосаОфлайна = () => выполнить(`(() => { const п = document.getElementById('офлайн-полоса');
  if (!п) return null;
  const ч = ид => Number((document.getElementById(ид) || {}).innerText ?? -1);
  return {on: п.dataset.on, текст: п.innerText, локальных: ч('сеть-локальных'), внешних: ч('сеть-внешних'),
          заблокировано: ч('сеть-заблокировано')}; })()`);
const переключитьОфлайн = async (вкл) => {
  await выполнить(`(() => { const т = document.getElementById('офлайн-тумблер');
    if (т && т.checked !== ${вкл}) т.click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < 15000) {
    await new Promise(р => setTimeout(р, 500));
    const п = await полосаОфлайна();
    if (п && п.on === (вкл ? '1' : '0')) return п;
  }
  ошибки.push(`переключатель офлайна не встал в ${вкл}`);
  return await полосаОфлайна();
};
// Ход реплики на странице: пока модель думает, снимается строка хода — этапы
// и счётчик токенов; итог — последний ответ в ленте.
const сказатьОфлайн = async (текст, секунд = 1500, наЭтап = null) => {
  const было = (await последнийОтвет() || {сообщений: 0}).сообщений;
  await выполнить(`(() => { document.getElementById('чат-реплика').value = ${JSON.stringify(текст)};
    document.getElementById('чат-отправить').click(); return 'ок'; })()`);
  const этапы = new Set();
  let пишет = 0;
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1500));
    const ход = await выполнить(`(() => [...document.querySelectorAll('#чат-ход .этап-строка')].map(с =>
      ({этап: с.dataset.этап, текст: с.innerText})))()`) || [];
    for (const с of ход) {
      if (!этапы.has(с.этап)) { этапы.add(с.этап); if (наЭтап) await наЭтап(с.этап, с.текст); }
      const м = /пишет: (\d+) ток/.exec(с.текст);
      if (м) пишет = Math.max(пишет, Number(м[1]));
    }
    const о = await последнийОтвет();
    if (о && о.сообщений > было) {
      const этапыОтвета = await выполнить(`(() => { const все = document.querySelectorAll('#чат-лента .сообщение.ассистент');
        const о = все[все.length - 1]; return (о.querySelector('.этапы') || {}).innerText || ''; })()`);
      return {...о, этапыХода: [...этапы], пишет, этапыОтвета, секунд: Math.round((Date.now() - начало) / 1000)};
    }
    const сбой = await выполнить(`(document.querySelector('#чат-ход .ошибка') || {}).innerText || ''`);
    if (сбой) { ошибки.push('чат: ' + сбой); return {ошибка: сбой, этапыХода: [...этапы], пишет}; }
  }
  ошибки.push('чат не ответил: ' + текст.slice(0, 60));
  return {ошибка: 'нет ответа', этапыХода: [...этапы], пишет};
};
const картаОфлайна = () => выполнить(`(() => ({
  роли: [...document.querySelectorAll('#офлайн-роли tr[data-роль]')].map(т => ({роль: т.dataset.роль, модель: т.children[1].innerText})),
  поиск: (document.getElementById('офлайн-поиск') || {}).innerText || '',
  серверы: (document.getElementById('офлайн-серверы') || {}).innerText || '',
  журнал: (document.getElementById('офлайн-журнал') || {}).innerText || '',
  состояние: (document.getElementById('офлайн-состояние') || {}).innerText || ''}))()`);
const ждатьКартуОфлайна = async () => {
  const начало = Date.now();
  while (Date.now() - начало < 20000) {
    await new Promise(р => setTimeout(р, 700));
    const к = await картаОфлайна();
    if (к.роли.length) return к;
  }
  ошибки.push('карточка офлайна на /local не нарисовалась');
  return await картаОфлайна();
};

// День 31: ассистент разработчика — /help на странице /chat (spec FR-09). Карточка ответа — то,
// что видит человек: источники документации проекта, ветка git, вызовы инструментов MCP.
const последняяПомощь = () => выполнить(`(() => {
  const все = document.querySelectorAll('#чат-лента .сообщение.помощь');
  const п = все[все.length - 1];
  if (!п) return {карточек: 0};
  return {карточек: все.length, неЗнаю: п.getAttribute('data-не-знаю') === '1',
          понято: (п.querySelector('.понято') || {}).innerText || '',
          текст: (п.querySelector('.текст') || {}).innerText || '',
          источники: [...п.querySelectorAll('.источник.документ .паспорт')].map(и => и.innerText),
          цитат: п.querySelectorAll('.цитата').length,
          вызовы: [...п.querySelectorAll('.вызов')].map(в => в.innerText),
          предупреждения: [...п.querySelectorAll('.предупреждение')].map(в => в.innerText)};
})()`);
const спроситьПомощь = async (текст, секунд = 600) => {
  const было = (await последняяПомощь()).карточек;
  await выполнить(`(() => { const поле = document.getElementById('чат-реплика'); поле.value = ${JSON.stringify(текст)};
    document.getElementById('чат-отправить').click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1000));
    const о = await последняяПомощь();
    if (о.карточек > было) return о;
    const сбой = await выполнить(`(document.querySelector('#чат-ход .ошибка') || {}).innerText || ''`);
    if (сбой) { ошибки.push('/help: ' + сбой); return {ошибка: сбой}; }
  }
  ошибки.push('/help не ответил: ' + текст.slice(0, 60));
  return {ошибка: 'нет ответа'};
};

if (РЕЖИМ === 'ассистент') {
  await наСтраницеЧата('/chat');
  await открытьСессию('проверка-помощь');
  await новыйРазговорНаСтранице();
  const подсказка = await выполнить(`(document.getElementById('подсказка-помощи') || {}).innerText || ''`);
  console.log('подсказка:', подсказка.replace(/\s+/g, ' ').slice(0, 120));
  if (!/\/help вопрос/.test(подсказка)) ошибки.push('нет подсказки про /help под полем ввода');

  await выполнить(`(() => { document.getElementById('чат-реплика').value = '/help';
    document.getElementById('чат-отправить').click(); return 'ок'; })()`);
  let справка = '';
  for (let i = 0; i < 30 && !справка; i++) {
    await new Promise(р => setTimeout(р, 500));
    справка = await выполнить(`(document.querySelector('#чат-ход .справка-помощи') || {}).innerText || ''`);
  }
  console.log('/help:', справка.split('\n')[1] || справка.slice(0, 100));
  if (!/\/help вопрос/.test(справка)) ошибки.push('/help без вопроса — нет справки');

  const о1 = await спроситьПомощь('/help Какой стек у проекта?');
  console.log('стек:', (о1.текст || '').slice(0, 120), '|', (о1.источники || []).slice(0, 3).join('; '), '|', (о1.понято || '').slice(0, 90));
  if (!/Django/.test(о1.текст || '') || !(о1.источники || []).length || !/ветка/.test(о1.понято || '') || !о1.цитат)
    ошибки.push('стек: нет ответа с источником, цитатой и веткой');

  const о2 = await спроситьПомощь('/help Какие модели данных есть в приложении org?');
  console.log('модели:', (о2.текст || '').slice(0, 120), '|', (о2.источники || []).slice(0, 3).join('; '));
  if (!/Unit/.test(о2.текст || '') || !(о2.источники || []).some(и => /models\.py/.test(и))) ошибки.push('модели org: нет models.py в источниках');

  const о3 = await спроситьПомощь('/help Что изменилось в последнем коммите?');
  console.log('коммит:', (о3.текст || '').slice(0, 120), '|', (о3.вызовы || []).join('; ').slice(0, 160));
  if (!(о3.вызовы || []).some(в => /git_(diff|log)/.test(в))) ошибки.push('последний коммит: нет вызова git_diff / git_log');

  const о4 = await спроситьПомощь('/help Закоммить мои изменения и отправь в origin');
  console.log('опасная:', (о4.текст || '').slice(0, 140));
  if (!/не могу|нельзя|только (на )?чтение|нет такого инструмента|такого инструмента нет/i.test(о4.текст || ''))
    ошибки.push('опасная просьба: нет отказа');

  await наСтраницеЧата('/chat');
  await открытьСессию('проверка-помощь');
  const после = await последняяПомощь();
  console.log('после перезагрузки карточек:', после.карточек);
  if (после.карточек !== 4) ошибки.push(`после перезагрузки карточек ${после.карточек}, а не 4`);
  console.log('\n=== итог ===');
  console.log('исключений JS и провалов:', ошибки.length ? ошибки : 'нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// День 32: AI-ревью ветки — /review на странице /chat (spec FR-13). Карточка — вердикт, три раздела,
// находки с файлом и строкой; ход — этапы конвейера под полем ввода.
const последнееРевью = () => выполнить(`(() => {
  const все = document.querySelectorAll('#чат-лента .сообщение.ревью');
  const р = все[все.length - 1];
  if (!р) return {карточек: 0};
  return {карточек: все.length, вердикт: р.getAttribute('data-вердикт'),
          понято: (р.querySelector('.понято') || {}).innerText || '',
          разделы: [...р.querySelectorAll('.источники .заголовок')].map(з => з.innerText),
          находки: [...р.querySelectorAll('.находка')].map(н => ({раздел: н.dataset.раздел, серьёзность: н.dataset.серьёзность,
                                                                текст: н.innerText.slice(0, 200)})),
          опора: р.querySelectorAll('.находка .мета').length,
          мета: (р.querySelector(':scope > .мета:last-child') || {}).innerText || ''};
})()`);

if (РЕЖИМ === 'ревью') {
  const диапазон = process.env.REVIEW_RANGE || 'main...HEAD';
  // Подписи — только в записи показа (SHOW_CAPTIONS): проверка без записи идёт без них.
  const подписьРевью = (текст) => !process.env.SHOW_CAPTIONS ? null : выполнить(`(() => {
    let п = document.getElementById('подпись-показа');
    if (!п) { п = document.createElement('div'); п.id = 'подпись-показа';
      п.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;background:#0f172a;color:#f8fafc;' +
        'padding:10px 18px;font:16px/1.45 system-ui,sans-serif'; document.body.appendChild(п); }
    п.textContent = ${JSON.stringify('')} + ${JSON.stringify(текст)}; return 1; })()`);
  const ждатьПодпись = мс => process.env.SHOW_CAPTIONS ? new Promise(r => setTimeout(r, мс)) : null;
  await наСтраницеЧата('/chat');
  await открытьСессию('проверка-ревью');
  await новыйРазговорНаСтранице();
  const подсказка = await выполнить(`(document.getElementById('подсказка-помощи') || {}).innerText || ''`);
  if (!/\/review/.test(подсказка)) ошибки.push('нет подсказки про /review под полем ввода');
  const абзац = await выполнить(`(document.getElementById('день-32') || {}).innerText || ''`);
  if (!/AI-ревью/.test(абзац)) ошибки.push('нет абзаца о Дне 32');
  const было = (await последнееРевью()).карточек;
  await подписьРевью('Сцена 5 — тот же конвейер в чате агента (веб, стенд 5001): /review диапазон — ревью ветки, как PR; ход — по этапам под полем ввода');
  await выполнить(`(() => { const п = document.getElementById('чат-реплика'); п.value = ${JSON.stringify('/review ' + диапазон)};
    п.scrollIntoView({block: 'center'}); return 'ок'; })()`);
  await ждатьПодпись(3000);
  await выполнить(`(() => { document.getElementById('чат-отправить').click(); return 'ок'; })()`);
  const этапы = new Set();
  let р = {карточек: было};
  const начало = Date.now();
  while (Date.now() - начало < 600000) {
    await new Promise(r => setTimeout(r, 1000));
    for (const э of await выполнить(`[...document.querySelectorAll('#чат-ход .этап-строка')].map(с => с.innerText)`)) этапы.add(э.replace(/\s+\d+ с.*$/, ''));
    р = await последнееРевью();
    if (р.карточек > было) break;
    const сбой = await выполнить(`(document.querySelector('#чат-ход .ошибка') || {}).innerText || ''`);
    if (сбой) { ошибки.push('/review: ' + сбой); break; }
  }
  console.log('этапы хода:', [...этапы].join(' | ').slice(0, 300));
  console.log(`карточка: ${р.вердикт}; ${(р.понято || '').slice(0, 100)}`);
  console.log('разделы:', (р.разделы || []).join('; '));
  for (const н of (р.находки || []).slice(0, 6)) console.log(`  [${н.раздел}/${н.серьёзность}] ${н.текст.replace(/\s+/g, ' ').slice(0, 140)}`);
  console.log('мета:', (р.мета || '').replace(/\s+/g, ' ').slice(0, 160));
  if (process.env.SHOW_CAPTIONS) {
    await выполнить(`(() => { const все = document.querySelectorAll('#чат-лента .сообщение.ревью');
      const р = все[все.length - 1]; if (р) р.scrollIntoView({behavior: 'smooth', block: 'start'}); return 1; })()`);
    await подписьРевью(`Сцена 5 — карточка: вердикт ${р.вердикт}, разделы «баги», «архитектура», «рекомендации», находки с файлом и строкой, опора; в истории чата — итог ревью`);
    await ждатьПодпись(6000);
    for (let i = 0; i < 4; i++) { await выполнить(`(() => { document.getElementById('чат-лента').scrollBy({top: 420, behavior: 'smooth'}); return 1; })()`); await ждатьПодпись(2500); }
    await остановитьЗапись();
  }
  if (р.карточек <= было) ошибки.push('/review: карточка не появилась');
  if ((р.разделы || []).length !== 3) ошибки.push('/review: в карточке не три раздела');
  if (process.env.REVIEW_EXPECT && р.вердикт !== process.env.REVIEW_EXPECT) ошибки.push(`/review: вердикт ${р.вердикт}, ожидался ${process.env.REVIEW_EXPECT}`);
  if (process.env.REVIEW_FILE && !(р.находки || []).some(н => н.текст.includes(process.env.REVIEW_FILE)))
    ошибки.push(`/review: нет находки по ${process.env.REVIEW_FILE}`);
  if (![...этапы].some(э => /ревью/.test(э))) ошибки.push('/review: под полем ввода не было хода по этапам');
  await наСтраницеЧата('/chat');
  await открытьСессию('проверка-ревью');
  const после = await последнееРевью();
  console.log('после перезагрузки карточек ревью:', после.карточек);
  if (после.карточек !== 1) ошибки.push(`после перезагрузки карточек ревью ${после.карточек}, а не 1`);
  console.log('\n=== итог ===');
  console.log('исключений JS и провалов:', ошибки.length ? ошибки : 'нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

if (РЕЖИМ === 'офлайн') {
  const п0 = await полосаОфлайна();
  if (!п0) ошибки.push('на главной нет полосы офлайна');
  console.log('полоса до:', п0 && п0.on, п0 && п0.текст.replace(/\s+/g, ' ').slice(0, 160));
  const п1 = await переключитьОфлайн(true);
  console.log('полоса после включения:', п1 && п1.on, п1 && п1.текст.replace(/\s+/g, ' ').slice(0, 160));
  const облачные = await выполнить(`[...document.querySelectorAll('#модель option[data-local="0"]')].map(о => о.disabled)`);
  const локальные = await выполнить(`[...document.querySelectorAll('#модель option[data-local="1"]')].filter(о => !о.disabled).length`);
  console.log(`главная: облачных моделей в списке ${облачные.length}, серых ${облачные.filter(Boolean).length}; локальных доступно ${локальные}`);
  if (!облачные.length || !облачные.every(Boolean)) ошибки.push('в офлайне облачные модели в списке не серые');
  if (локальные < 5) ошибки.push('в офлайне доступно локальных моделей меньше пяти');

  await наСтраницеЛокальной('/local');
  const к = await ждатьКартуОфлайна();
  console.log('карта ролей:', к.роли.map(р => `${р.роль}=${р.модель.split(' (')[0]}`).join('; '));
  console.log('поиск:', к.поиск.replace(/\s+/g, ' ').slice(0, 220));
  console.log('серверы:', к.серверы.replace(/\s+/g, ' ').slice(0, 200));
  if (к.роли.length !== 14 || !к.роли.every(р => /\(локально\)/.test(р.модель))) ошибки.push('не все роли в карте — локальные модели');
  if (!/кросс/.test(к.поиск)) ошибки.push('в карточке нет реранкера кросс-энкодера');
  if (!/✗ github/.test(к.серверы) || !/✓ tracker/.test(к.серверы)) ошибки.push('серверы MCP в офлайне показаны не так');
  const режимЛок = await выполнить(`(() => { const с = document.getElementById('лок-режим');
    return {значение: с.value, облако: [...с.querySelectorAll('option[data-облако]')].every(о => о.disabled)}; })()`);
  if (режимЛок.значение !== 'local' || !режимЛок.облако) ошибки.push('на /local в офлайне доступны режимы с облаком');

  await наСтраницеЧата();
  await открытьСессию('офлайн-браузер');
  await новыйРазговорНаСтранице();
  const о = await сказатьОфлайн(ОФЛАЙН_РЕПЛИКА, 1500, (этап, текст) => console.log(`  … этап на странице: ${текст.replace(/\s+/g, ' ').slice(0, 140)}`));
  console.log(`ответ за ${о.секунд} с: ${(о.текст || о.ошибка || '').slice(0, 160)}`);
  console.log('этапы хода, увиденные на странице:', (о.этапыХода || []).join(' → '), '· счётчик «пишет» дошёл до', о.пишет);
  console.log('строка этапов у ответа:', (о.этапыОтвета || '').replace(/\s+/g, ' ').slice(0, 300));
  if (!(о.этапыХода || []).includes('разбор-реплики') || !(о.этапыХода || []).includes('ответ-по-документам'))
    ошибки.push('страница не показала ход по этапам');
  if (!(о.пишет > 0)) ошибки.push('страница не показала счётчик токенов');
  if (!/20/.test(о.текст || '') || !(о.документов >= 1) || !(о.цитат >= 1) || !(о.сChunkId >= 1))
    ошибки.push('ответ офлайн-чата без «20», источника с chunk_id или цитаты');
  if (!/локально/.test(о.этапыОтвета || '') || /облако/.test(о.этапыОтвета || '')) ошибки.push('в этапах ответа не только локальные модели');
  await new Promise(р => setTimeout(р, 4500));
  const п2 = await полосаОфлайна();
  console.log(`сеть после реплики: локальных ${п2.локальных}, внешних ${п2.внешних}, заблокировано ${п2.заблокировано}`);
  if (п2.внешних !== 0 || !(п2.локальных > 0)) ошибки.push('в офлайне есть внешние запросы или нет локальных');

  const п3 = await переключитьОфлайн(false);
  console.log('полоса после выключения:', п3 && п3.on);
  if (!п3 || п3.on !== '0') ошибки.push('офлайн не выключился');
  console.log(ошибки.length ? 'ошибки:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

if (РЕЖИМ === 'офлайн-инструменты') {
  await переключитьОфлайн(true);
  // С чистого диалога: в истории стенда бывают ответы о других задачах, и 4B
  // однажды ответила по ним, не вызвав инструмент (см. RESULTS.md).
  await выполнить(`(() => { document.getElementById('забыть').click(); return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 1500));
  await выполнить(`(() => { document.getElementById('модель').value = '';
    document.getElementById('инструменты-серверы').value = 'tracker';
    const г = document.getElementById('инструменты-вкл');
    г.checked = true; г.dispatchEvent(new Event('change')); return 'ок'; })()`);
  const предел = Date.now() + 120000;
  let итог = '';
  while (Date.now() < предел) {
    await new Promise(р => setTimeout(р, 1000));
    итог = await выполнить(`document.getElementById('инструменты-итог').innerText`);
    if (итог && !итог.includes('подключаюсь')) break;
  }
  console.log('инструменты включены:', итог);
  await выполнить(`(() => { document.getElementById('ввод').value = ${JSON.stringify(ОФЛАЙН_MCP)};
    document.getElementById('отправить').click(); return 'ок'; })()`);
  const видели = new Set();
  const начало = Date.now();
  let ответ = '';
  while (Date.now() - начало < 1800000) {
    await new Promise(р => setTimeout(р, 2000));
    const ход = await выполнить(`(document.getElementById('ход-агента') || {}).textContent || ''`);
    for (const кусок of ход.split(' → ')) {
      const ключ = кусок.replace(/^… \d+ с · /, '').replace(/ · пишет: \d+ ток\.$/, '');
      if (ключ && !видели.has(ключ)) { видели.add(ключ); console.log('  … ход на странице:', ход.slice(0, 200)); }
    }
    const есть = await выполнить(`document.querySelectorAll('#чат .вызов').length`);
    if (есть && !(await выполнить(`!!document.getElementById('ход-агента')`))) {
      ответ = await выполнить(`(() => { const у = Array.from(document.querySelectorAll('#чат .от-агента')).pop();
        return у ? у.innerText : ''; })()`);
      break;
    }
    const сбой = await выполнить(`(() => { const у = Array.from(document.querySelectorAll('#чат .ошибка')).pop();
      return у ? у.innerText : ''; })()`);
    if (сбой) { ошибки.push('агент: ' + сбой); break; }
  }
  const вызовы = await выполнить(`Array.from(document.querySelectorAll('#чат .вызов')).map(у => у.innerText.slice(0, 120))`);
  const расход = await выполнить(`(() => { const у = Array.from(document.querySelectorAll('#чат .системное'))
    .filter(с => /: [\\d\\s]+ токенов/.test(с.innerText)).pop();
    return у ? у.innerText : ''; })()`);
  console.log(`ответ за ${Math.round((Date.now() - начало) / 1000)} с: ${(ответ || '').replace(/\s+/g, ' ').slice(0, 220)}`);
  console.log('вызовы:', (вызовы || []).join(' | '), '· расход:', расход);
  if (![...видели].some(к => /круг 1 · loc-qwen35 \(локально\)/.test(к))) ошибки.push('строка хода не показала круг на локальной модели');
  if (!(вызовы || []).some(в => /tracker__/.test(в))) ошибки.push('нет строки вызова инструмента трекера');
  if (!/открыт/i.test(ответ || '') || !/низк/i.test(ответ || '')) ошибки.push('в ответе нет статуса «Открыта» и приоритета «Низкий»');
  if (!/^loc-qwen35:/.test(расход || '')) ошибки.push('ответил не loc-qwen35: ' + расход);
  await new Promise(р => setTimeout(р, 4500));
  const п = await полосаОфлайна();
  console.log(`сеть: локальных ${п.локальных}, внешних ${п.внешних}`);
  if (п.внешних !== 0) ошибки.push('в офлайне есть внешние запросы');
  await выполнить(`(() => { const г = document.getElementById('инструменты-вкл'); г.checked = false;
    г.dispatchEvent(new Event('change')); return 'ок'; })()`);
  await переключитьОфлайн(false);
  console.log(ошибки.length ? 'ошибки:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

if (РЕЖИМ === 'локально') {
  if (!(await выполнить(`!!document.getElementById('лок-ссылка-26')`))) ошибки.push('на главной нет ссылки на /local');
  // Локальная модель — провайдер каталога: в общем списке моделей главной она
  // доступна для выбора, а не серая «нет ключа».
  const вСписке = await выполнить(`[...(document.getElementById('модель') || {options: []}).options]
    .filter(о => о.value.startsWith('loc-') && !о.disabled).length`);
  console.log(`в списке моделей главной доступно локальных: ${вСписке}`);
  if (вСписке < 5) ошибки.push(`в списке моделей главной доступно локальных ${вСписке}, а не 5`);
  await наСтраницеЛокальной('/local');
  const заголовок = await выполнить('document.title');
  if (!заголовок.startsWith('Локальная модель')) ошибки.push('заголовок /local: ' + заголовок);
  const карточка = await выполнить(`document.getElementById('ollama').innerText`);
  console.log('ollama:', карточка.split('\n').slice(0, 3).join(' | ').slice(0, 300));
  if (!/служба ollama: active/.test(карточка)) ошибки.push('служба ollama не active');
  if (!/петлевой адрес/.test(карточка)) ошибки.push('не показан петлевой адрес');
  const моделей = await выполнить(`document.querySelectorAll('#модели-ollama tr[data-модель]').length`);
  console.log('скачанных моделей в таблице:', моделей);
  if (моделей < 5) ошибки.push('в таблице меньше пяти моделей');

  const простые = await спроситьНаЛокальной(ВОПРОС_ПРОСТОЙ);
  for (const о of простые) console.log(`простой ${о.локально ? 'локально' : 'облако'}: ${о.текст.slice(0, 60)} | ${о.итог.replace(/\n/g, ' ').slice(0, 200)}`);
  if (простые.length !== 2 || !простые.every(о => /Париж/i.test(о.текст))) ошибки.push('«оба рядом»: не оба ответили «Париж»');
  const лок = простые.find(о => о.локально);
  if (!лок || !/Ollama: загрузка в память/.test(лок.итог)) ошибки.push('у локального ответа нет замеров Ollama');
  await выполнить(`document.getElementById('обновить').click()`);
  await new Promise(р => setTimeout(р, 2000));
  const память = await выполнить(`document.getElementById('в-памяти').innerText`);
  console.log('в памяти:', память.replace(/\n/g, ' | ').slice(0, 200));
  if (!/100% CPU/.test(память)) ошибки.push('модель в памяти не показана на CPU');

  const средний = await спроситьНаЛокальной(ВОПРОС_СРЕДНИЙ, {режим: 'local'});
  console.log('средний:', средний.map(о => о.текст.slice(0, 80)).join(' | '));
  if (!средний.length || !/36/.test(средний[0].текст)) ошибки.push('средний: нет 36');

  for (const вид of ['cli', 'native', 'openai']) {
    const р = await доступНаСтранице(вид);
    console.log(`${вид}:`, (р.итог || р.ошибка || '').replace(/\n/g, ' ').slice(0, 160), '|', (р.вывод || '').replace(/\n/g, ' ').slice(0, 120));
    if (р.ошибка || !/391/.test((р.вывод || '') + (р.итог || ''))) ошибки.push(`${вид}: нет ответа 391`);
  }

  const бенч = await выполнить(`(document.getElementById('бенч-итог') || {}).innerText || ''`);
  console.log('замер:', бенч.split('\n').slice(0, 3).join(' | ').slice(0, 300));
  if (!/верно/.test(бенч)) ошибки.push('нет таблицы замера');
  if (!(await выполнить(`!!document.getElementById('бенч-выбор')`))) ошибки.push('не показан выбор основной модели');
  const отметка = await выполнить(`(async () => {
    const д = document.querySelector('#бенч details'); if (!д) return 'нет';
    д.open = true; const к = д.querySelector('.отметки button[data-оценка="👍"]'); if (!к) return 'нет кнопки';
    к.click(); await new Promise(р => setTimeout(р, 1500));
    return к.classList.contains('выбрана') ? 'ок' : 'не отмечено'; })()`);
  console.log('отметка 👍:', отметка);
  if (отметка !== 'ок') ошибки.push('отметка 👍 не сохранилась: ' + отметка);

  const теги = await сырыеJSON('/api/tags');
  console.log('сырой /api/tags:', теги.replace(/\s+/g, ' ').slice(0, 160));
  if (!/"models"/.test(теги) || !/qwen3/.test(теги)) ошибки.push('сырой /api/tags не открылся');
  await наСтраницеЛокальной(АДРЕС + '/local');
  console.log(ошибки.length ? 'ОШИБКИ:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 28: локальный RAG рядом с облаком -------------------------------------
// «локальный-rag» — офлайн, вопрос из набора, ход по этапам на странице, ответ с
// цитатой, замеры по слоям, внешних запросов 0. «сравнение» — те же фрагменты у
// облака: сначала отказ в офлайне, потом четыре колонки и отметка 👍.

const ВОПРОС_RAG = 'c01';   // срок ежемесячной оплаты по договору VPN — ожидание «20 числа»
const наСтраницеRAG = async () => {
  await выполнить(`(() => { location.href = '/rag'; return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < 30000) {
    await new Promise(р => setTimeout(р, 700));
    if (await выполнить(`!!document.getElementById('лрг-спросить') && !!document.getElementById('офлайн-полоса')`)) return true;
  }
  ошибки.push('страница /rag не загрузилась');
  return false;
};
const выбратьВопросRAG = (ид) => выполнить(`(() => { const н = document.getElementById('лрг-набор');
  н.value = ${JSON.stringify(ид)}; н.dispatchEvent(new Event('change'));
  return document.getElementById('лрг-вопрос').value; })()`);
const ходRAG = () => выполнить(`[...document.querySelectorAll('#лрг-ход .этап')].map(э => ({этап: э.dataset['этап'], текст: э.innerText}))`);
// Ждать узел итога; по дороге снимать ход — этапы и счётчик токенов.
const ждатьRAG = async (признак, секунд, наЭтап = null) => {
  const этапы = new Map();
  let пишет = 0;
  const начало = Date.now();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1500));
    for (const э of (await ходRAG()) || []) {
      // Ключ — весь текст этапа без тикающих секунд: у двух локальных участников
      // начало подписи одинаковое («локально · …»), и по нему второй потерялся бы.
      const ключ = э.текст.replace(/ · [\d,]+ с$/, '') + '|' + э.этап;
      if (!этапы.has(ключ)) { этапы.set(ключ, э); if (наЭтап) await наЭтап(э.текст); }
    }
    const т = await выполнить(`(document.getElementById('лрг-токены') || {}).innerText || ''`);
    const м = /пишет [^:]+: (\d+) ток/.exec(т || '');
    if (м) пишет = Math.max(пишет, Number(м[1]));
    if (await выполнить(`!!document.getElementById(${JSON.stringify(признак)})`))
      return {этапы: [...этапы.values()], пишет, секунд: Math.round((Date.now() - начало) / 1000)};
    const сбой = await выполнить(`(document.querySelector('#лрг-ответ .ошибка') || {}).innerText || ''`);
    if (сбой) return {ошибка: сбой, этапы: [...этапы.values()], пишет};
  }
  return {ошибка: 'нет ответа', этапы: [...этапы.values()], пишет};
};
const итогЛокальногоRAG = () => выполнить(`(() => { const и = document.getElementById('лрг-итог'); if (!и) return null;
  const сеть = и.querySelector('[data-внешних]');
  return {текст: (и.querySelector('.текст-ответа') || {}).innerText || '',
          источников: и.querySelectorAll('.источник.процитирован').length,
          дословно: [...и.querySelectorAll('.цитата')].filter(ц => ц.dataset['проверка'] === 'дословно').length,
          замеры: [...и.querySelectorAll('.замеры')].map(з => з.innerText).join(' | '),
          внешних: сеть ? сеть.dataset['внешних'] : null, шапка: (и.querySelector('.мета') || {}).innerText || ''}; })()`);
const колонкиRAG = () => выполнить(`[...document.querySelectorAll('#лрг-колонки .колонка')].map(к => {
  const сеть = к.querySelector('[data-внешних]'), з = к.querySelector('.замеры[data-всего]');
  return {участник: к.dataset['участник'], верно: к.dataset['верно'], внешних: сеть ? сеть.dataset['внешних'] : null,
          всего: з ? з.dataset['всего'] : null, текст: ((к.querySelector('.текст-ответа') || к.querySelector('.не-знаю') || {}).innerText || '').slice(0, 120)}; })`);

if (РЕЖИМ === 'локальный-rag') {
  await наСтраницеRAG();
  const п1 = await переключитьОфлайн(true);
  console.log('офлайн:', п1 && п1.on, '· внешних до вопроса:', п1 && п1.внешних);
  console.log('вопрос:', await выбратьВопросRAG(ВОПРОС_RAG));
  await выполнить(`(() => { document.getElementById('лрг-спросить').click(); return 'ок'; })()`);
  const х = await ждатьRAG('лрг-итог', 1800, т => console.log('  … ' + т.replace(/\s+/g, ' ').slice(0, 200)));
  console.log(`ответ за ${х.секунд} с · счётчик «пишет» дошёл до ${х.пишет}`);
  if (х.ошибка) ошибки.push('локальный RAG: ' + х.ошибка);
  const и = await итогЛокальногоRAG() || {};
  console.log('ответ:', (и.текст || '').slice(0, 200));
  console.log('шапка:', (и.шапка || '').replace(/\s+/g, ' ').slice(0, 200));
  console.log('замеры:', (и.замеры || '').replace(/\s+/g, ' ').slice(0, 400));
  console.log(`источников ${и.источников}, цитат «дословно» ${и.дословно}, внешних за ответ ${и.внешних}`);
  const этапы = х.этапы.map(э => э.этап);
  if (!этапы.includes('поиск') || !этапы.includes('ответ')) ошибки.push('страница не показала этапы «поиск» и «ответ»');
  if (!(х.пишет > 0)) ошибки.push('страница не показала счётчик токенов');
  if (!/20/.test(и.текст || '') || !(и.источников >= 1) || !(и.дословно >= 1)) ошибки.push('ответ без «20», источника или дословной цитаты');
  if (!/кросс-энкодер/.test(и.замеры || '') || !/чтение промпта/.test(и.замеры || '')) ошибки.push('в замерах нет поиска по шагам или чтения промпта');
  if (и.внешних !== '0') ошибки.push('у локального ответа есть внешние запросы');
  if (!/loc-qwen35/.test(и.шапка || '')) ошибки.push('ответила не локальная модель');
  await new Promise(р => setTimeout(р, 4500));
  const п2 = await полосаОфлайна();
  console.log(`полоса после ответа: локальных ${п2.локальных}, внешних ${п2.внешних}, заблокировано ${п2.заблокировано}`);
  if (п2.внешних !== 0) ошибки.push('в офлайне есть внешние запросы');
  const п3 = await переключитьОфлайн(false);
  if (!п3 || п3.on !== '0') ошибки.push('офлайн не выключился');
  console.log(ошибки.length ? 'ошибки:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

if (РЕЖИМ === 'сравнение') {
  await наСтраницеRAG();
  await переключитьОфлайн(true);
  await выбратьВопросRAG(ВОПРОС_RAG);
  await выполнить(`(() => { document.getElementById('лрг-рядом').click(); return 'ок'; })()`);
  const запрет = await ждатьТекст('лрг-ответ', 'выключенным офлайном', 30);
  console.log('в офлайне «Рядом с облаком»:', запрет >= 0 ? 'объяснил, что облако нужно включить' : 'НЕТ объяснения');
  if (запрет < 0) ошибки.push('в офлайне «Рядом с облаком» не объяснил отказ');
  await переключитьОфлайн(false);
  await выполнить(`(() => { document.getElementById('лрг-рядом').click(); return 'ок'; })()`);
  const х = await ждатьRAG('лрг-колонки', 2700, т => console.log('  … ' + т.replace(/\s+/g, ' ').slice(0, 200)));
  console.log(`четыре участника за ${х.секунд} с`);
  if (х.ошибка) ошибки.push('рядом с облаком: ' + х.ошибка);
  const колонки = await колонкиRAG() || [];
  for (const к of колонки) console.log(`  ${к.участник}: верно ${к.верно} · всего ${к.всего} с · внешних ${к.внешних} · ${к.текст.replace(/\s+/g, ' ')}`);
  if (колонки.map(к => к.участник).join() !== 'лок-4b,лок-1.7b,облако-тот-же,облако-лучшее') ошибки.push('колонки не те: ' + колонки.map(к => к.участник).join());
  for (const к of колонки) {
    const локальный = к.участник.startsWith('лок');
    if (локальный && к.внешних !== '0') ошибки.push(`у ${к.участник} есть внешние запросы`);
    if (!локальный && !(Number(к.внешних) > 0)) ошибки.push(`у ${к.участник} журнал не увидел облака`);
  }
  await выполнить(`(() => { const к = document.querySelector('#лрг-колонки .колонка[data-участник="лок-4b"] button[data-отметка="👍"]');
    if (к) к.click(); return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 2000));
  const выбрана = await выполнить(`!!document.querySelector('#лрг-колонки .колонка[data-участник="лок-4b"] button.выбрана[data-отметка="👍"]')`);
  const отметки = await выполнить(`fetch('/api/rag/localbench').then(о => о.json()).then(с => с.marks)`);
  console.log('отметка 👍 у лок-4b:', выбрана ? 'на странице' : 'НЕТ', '· на сервере:', отметки && отметки['c01|лок-4b']);
  if (!выбрана || !отметки || отметки['c01|лок-4b'] !== '👍') ошибки.push('отметка 👍 не сохранилась');
  const сводка = await выполнить(`(document.getElementById('ср-сводка') || {}).innerText || ''`);
  console.log('карточка сравнения:', сводка.replace(/\s+/g, ' ').slice(0, 300));
  if (!сводка) ошибки.push('карточка сравнения пуста');
  console.log(ошибки.length ? 'ошибки:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 29: оптимизация под задачу (карточка на /local) ---------------------------
const наКарточкеОпт = async () => {
  await наСтраницеЛокальной('/local');
  const начало = Date.now();
  while (Date.now() - начало < 30000) {
    await new Promise(р => setTimeout(р, 700));
    if (await выполнить(`document.querySelectorAll('#опт-конфигурации tr').length > 1`)) return true;
  }
  ошибки.push('карточка «Оптимизация под задачу» не нарисовалась');
  return false;
};
const состояниеОпт = () => выполнить(`fetch('/api/opt').then(о => о.json())`);
const сервер2НаСтранице = async (действие, переменные = '') => {
  if (переменные) await выполнить(`(() => { document.getElementById('опт-переменные').value = ${JSON.stringify(переменные)}; return 'ок'; })()`);
  await выполнить(`(() => { document.getElementById(${JSON.stringify(действие === 'старт' ? 'опт-сервер-старт' : 'опт-сервер-стоп')}).click(); return 'ок'; })()`);
  const начало = Date.now();
  while (Date.now() - начало < 40000) {
    await new Promise(р => setTimeout(р, 1000));
    const т = await выполнить(`(document.getElementById('опт-сервер') || {}).innerText || ''`);
    if (действие === 'старт' ? /^работает/.test(т) : /не запущен/.test(т)) return т;
  }
  return await выполнить(`(document.getElementById('опт-сервер') || {}).innerText || ''`);
};
const рядомНаСтранице = async (вопрос, а, б, секунд = 2400, наСобытие = null) => {
  await выполнить(`(() => {
    const свой = document.getElementById('опт-свой'), сел = document.getElementById('опт-вопрос');
    if ([...сел.options].some(о => о.value === ${JSON.stringify(вопрос)})) { сел.value = ${JSON.stringify(вопрос)}; свой.value = ''; }
    else свой.value = ${JSON.stringify(вопрос)};
    document.getElementById('опт-а').value = ${JSON.stringify(а)};
    document.getElementById('опт-б').value = ${JSON.stringify(б)};
    document.getElementById('опт-рядом').click(); return 'ок'; })()`);
  const начало = Date.now();
  const видели = new Set();
  while (Date.now() - начало < секунд * 1000) {
    await new Promise(р => setTimeout(р, 1500));
    const колонки = await выполнить(`[...document.querySelectorAll('#опт-ответы .ответ')].map(о => ({
      конф: о.dataset.конф, ход: (о.querySelector('.ход-конф') || {}).innerText || '',
      текст: (о.querySelector('.текст') || {}).innerText || '', итог: (о.querySelector('.итог') || {}).innerText || ''}))`) || [];
    for (const к_ of колонки) {
      const ключ = к_.конф + ':' + (к_.ход.match(/^(загрузка|ответ|пишет|верно|неверно)/) || [''])[0];
      if (наСобытие && !видели.has(ключ) && ключ.split(':')[1]) { видели.add(ключ); await наСобытие(к_); }
    }
    const сбой = await выполнить(`(document.querySelector('#опт-ответы .ошибка') || {}).innerText || ''`);
    if (сбой) return {ошибка: сбой, колонки, секунд: Math.round((Date.now() - начало) / 1000)};
    if (колонки.length === 2 && колонки.every(к_ => к_.итог)) return {колонки, секунд: Math.round((Date.now() - начало) / 1000)};
  }
  return {ошибка: 'нет ответа за отведённое время', колонки: []};
};

if (РЕЖИМ === 'оптимизация') {
  await наКарточкеОпт();
  const с = await состояниеОпт();
  const строк = await выполнить(`document.querySelectorAll('#опт-конфигурации tr').length - 1`);
  console.log(`конфигураций в таблице: ${строк}; нет весов: ${с.missing.join(', ') || 'нет'}; «после»: ${с.tuning.after}`);
  if (строк < 15) ошибки.push('в таблице конфигураций меньше 15 строк');
  if (с.missing.length) ошибки.push('не хватает весов: ' + с.missing.join(', '));
  const старт = await сервер2НаСтранице('старт', 'OLLAMA_FLASH_ATTENTION=1,OLLAMA_KV_CACHE_TYPE=q8_0');
  console.log('второй сервер:', старт.slice(0, 160));
  if (!/^работает/.test(старт) || !/q8_0/.test(старт)) ошибки.push('второй сервер не запустился с переменными');
  const стоп = await сервер2НаСтранице('стоп');
  console.log('второй сервер после стопа:', стоп);
  if (!/не запущен/.test(стоп)) ошибки.push('второй сервер не остановился');
  const после = с.tuning.configs.some(к_ => к_.имя === 'после') ? 'после' : 'р-шаблон';
  const р = await рядомНаСтранице('c01', 'до', после, 2400, к_ => console.log(`  … ${к_.конф}: ${к_.ход.slice(0, 120)}`));
  for (const к_ of р.колонки || []) {
    console.log(`${к_.конф}: ${к_.ход.slice(0, 80)} | ${к_.текст.slice(0, 80)} | ${к_.итог.replace(/\s+/g, ' ').slice(0, 220)}`);
  }
  if (р.ошибка) ошибки.push('вопрос в двух конфигурациях: ' + р.ошибка);
  else {
    if (!(р.колонки || []).every(к_ => /верно ✓/.test(к_.ход))) ошибки.push('не обе конфигурации ответили верно');
    if (!(р.колонки || []).every(к_ => /пик ОЗУ раннера \d+/.test(к_.итог) && /CPU [\d,]+ с/.test(к_.итог)))
      ошибки.push('у ответа нет памяти раннера или секунд процессора');
    if (!(р.колонки || []).every(к_ => /внешних 0/.test(к_.итог))) ошибки.push('у локального ответа есть внешние запросы');
  }
  const итоги = await выполнить(`(document.getElementById('опт-итоги') || {}).innerText || ''`);
  console.log('итоги:', итоги.split('\n').slice(0, 2).join(' | ').slice(0, 200));
  if (!/Скрининг рычагов/.test(итоги)) ошибки.push('нет таблицы скрининга');
  if (!/GPU-VPS/.test(итоги)) ошибки.push('нет сметы VPS');
  console.log(ошибки.length ? 'ошибки:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

// --- День 30: приватный AI-сервис ---------------------------------------------
// Страницы сервиса — /admin и /chat на https://<адрес в домашней сети>:8443 (показ — по
// живой службе) или :8444 (регрессия — по стенду со своей памятью, plan D-18). Ключи —
// из файла SERVICE_KEYS ({"админ", "чат", "документы", "лимит"}): в поля они вводятся
// как пароль, и в кадре видны точки, а не ключ.
const ключиСервиса = process.env.SERVICE_KEYS
  ? JSON.parse((await import('node:fs')).readFileSync(process.env.SERVICE_KEYS, 'utf-8')) : {};
const пауза = мс => new Promise(р => setTimeout(р, мс));
const ждатьНаСтранице = async (выражение, мс = 30000, шаг = 500) => {
  const конец = Date.now() + мс;
  while (Date.now() < конец) {
    const в = await выполнить(выражение);
    if (в) return в;
    await пауза(шаг);
  }
  return null;
};
const перейти = async (путь) => {
  await выполнить(`(() => { location.href = ${JSON.stringify(путь)}; return 'ок'; })()`);
  await пауза(1500);
  return ждатьНаСтранице(`document.readyState === 'complete' && location.pathname === ${JSON.stringify(путь)}`, 20000);
};
const админВойти = async (ключ) => {
  await ждатьНаСтранице(`!!document.getElementById('войти')`);
  await выполнить(`(() => { document.getElementById('ключ').value = ${JSON.stringify(ключ)};
    document.getElementById('войти').click(); return 'ок'; })()`);
  return ждатьНаСтранице(`(document.querySelector('#служба .большое') || {}).innerText || ''`, 20000);
};
const состояниеСервиса = (ключ) => выполнить(`fetch('/admin/api/state', {headers: {Authorization: 'Bearer ' +
  ${JSON.stringify(ключ)}}}).then(r => r.json())`);
const чатКлюч = (ключ, режим) => выполнить(`(() => { document.getElementById('ключ').value = ${JSON.stringify(ключ)};
  document.getElementById(${JSON.stringify('режим-' + режим)}).checked = true; return 'ок'; })()`);
const спроситьВЧате = async (текст, мс = 900000, наХод = null) => {
  await выполнить(`(() => { for (const id of ['последний-ответ', 'последний-отказ']) {
      const у = document.getElementById(id); if (у) у.removeAttribute('id'); }
    document.getElementById('вопрос').value = ${JSON.stringify(текст)};
    document.getElementById('отправить').click(); return 'ок'; })()`);
  const начало = Date.now();
  let прежний = '';
  while (Date.now() - начало < мс) {
    await пауза(1000);
    const с = await выполнить(`(() => { const о = document.getElementById('последний-ответ');
      const у = document.getElementById('последний-отказ');
      const все = к => о ? [...о.querySelectorAll('.' + к)].map(п => п.innerText) : [];
      return {ответ: о ? ((о.firstChild && о.firstChild.nodeValue) || '') : '', подписи: все('подпись'),
              источники: все('источник'), цитаты: все('цитата'), отказ: у ? у.innerText : '',
              статус: document.getElementById('статус').innerText, занят: document.getElementById('отправить').disabled}; })()`);
    if (наХод && с.статус && с.статус !== прежний) { прежний = с.статус; await наХод(с.статус); }
    if (с.отказ || (!с.занят && (с.подписи.length || с.ответ)))
      return {...с, секунд: Math.round((Date.now() - начало) / 1000)};
  }
  return {ошибка: 'нет ответа за отведённое время', подписи: [], источники: [], цитаты: [], ответ: '', отказ: ''};
};
// Лимит ключа — так, как его встретит клиент: семь запросов подряд одним ключом. Ответ сервиса
// показывается в ленте чата его же функцией (реплика, отказ — общие функции страницы).
const лимитНаСтранице = (ключ) => выполнить(`(async () => { const коды = []; let последний = null;
  for (let i = 0; i < 7; i++) {
    const r = await fetch('/v1/models', {headers: {Authorization: 'Bearer ' + ${JSON.stringify(ключ)}}});
    коды.push(r.status);
    if (r.status !== 200) последний = {код: r.status, тело: await r.json(), повтор: r.headers.get('retry-after') || ''};
  }
  if (последний) реплика('ошибка', 'GET /v1/models ×7 одним ключом: ' + коды.join(' ') + ' → ' +
    отказ(последний.код, последний.тело, new Headers({'retry-after': последний.повтор}))).id = 'последний-отказ';
  return {коды, код: последний && последний.тело.error.code}; })()`);
const ВОПРОС_ЧАТА = 'Что такое SLA? Ответь в двух предложениях.';
// Только вопрос из проверенных без ПД (local-opt.json → gpu.годные, День 29).
const ВОПРОС_ДОКУМЕНТЫ = 'До какого числа каждый месяц нужно оплачивать услуги связи по договору VPN с Ростелекомом?';

if (РЕЖИМ === 'сервис') {
  const К = ключиСервиса;
  if (!(К.админ && К.чат && К.документы && К.лимит)) ошибки.push('нет ключей SERVICE_KEYS');
  const служба = await админВойти(К.админ);
  const с = await состояниеСервиса(К.админ) || {};
  console.log(`служба: ${служба} · модель ${с.service?.модель} · ключей ${(с.keys || []).length} · клиентов ${(с.clients || []).length}`);
  if (!с.service || с.service.модель !== 'loc-rag-docs') ошибки.push('/admin не показал состояние сервиса');
  // Дополнение: карточка Telegram-бота — служба day31-tg, сеть бота, журнал без текстов, кнопки.
  const тг = await ждатьНаСтранице(`(() => { const с = document.getElementById('телеграм-служба'), н = document.getElementById('телеграм-сеть');
    return с && н ? {служба: с.innerText, сеть: н.innerText, кнопки: !!document.getElementById('телеграм-старт') && !!document.getElementById('телеграм-стоп')} : null; })()`, 20000);
  console.log('telegram-бот:', тг ? `${тг.служба} · ${тг.сеть.slice(0, 90)}` : 'карточки нет');
  if (!тг || !тг.кнопки || !/сеть бота/.test(тг.сеть)) ошибки.push('/admin: нет карточки Telegram-бота с сетью и кнопками');
  // День 31: карточка ассистента проекта — режим и переключатель облако / локально без перезапуска.
  const режимПроекта = () => ждатьНаСтранице(`(() => { const р = document.getElementById('проект-режим');
    return р ? р.innerText.trim() : null; })()`, 20000);
  const р0 = await режимПроекта();
  await выполнить(`document.getElementById('проект-локально').click(), 'ок'`);
  await new Promise(р => setTimeout(р, 2500));
  const р1 = await режимПроекта();
  await выполнить(`document.getElementById('проект-облако').click(), 'ок'`);
  await new Promise(р => setTimeout(р, 2500));
  const р2 = await режимПроекта();
  console.log(`ассистент проекта: ${р0} → «локально»: ${р1} → «облако»: ${р2}`);
  if (р1 !== 'локально' || р2 !== 'облако') ошибки.push('/admin: переключатель модели ассистента проекта не сработал');
  await перейти('/chat');
  await чатКлюч(К.чат, 'чат');
  const о = await спроситьВЧате(ВОПРОС_ЧАТА, 600000);
  console.log('чат:', (о.ответ || о.отказ).slice(0, 100), '|', о.подписи.join(' ').slice(0, 160));
  if (!о.ответ || !о.подписи.some(п => /вход \d+ · ответ \d+ токенов/.test(п))) ошибки.push('чат: нет ответа или подписи с токенами');
  await чатКлюч(К.чат, 'документы');
  const з = await спроситьВЧате(ВОПРОС_ДОКУМЕНТЫ, 60000);
  console.log('документы ключом «чат»:', з.отказ.slice(0, 100));
  if (!/HTTP 403/.test(з.отказ)) ошибки.push('ключ «чат» прошёл к документам');
  await чатКлюч(К.документы, 'документы');
  const д = await спроситьВЧате(ВОПРОС_ДОКУМЕНТЫ, 900000);
  console.log('документы:', д.ответ.slice(0, 100), '|', д.источники.join(' ').slice(0, 100), '|', д.цитаты.join(' ').slice(0, 100));
  if (!/\b20\b/.test(д.ответ) || !д.цитаты.some(ц => /✓ дословно/.test(ц))) ошибки.push('документы: нет «20 числа» с дословной цитатой');
  await чатКлюч(К.чат, 'чат');
  const б = await спроситьВЧате('Проверка предела. ' + 'слово '.repeat(4200), 60000);
  console.log('длинный вход:', б.отказ.slice(0, 140));
  if (!/HTTP 413/.test(б.отказ) || !/context_length_exceeded/.test(б.отказ)) ошибки.push('длинный вход не отклонён 413');
  const л = await лимитНаСтранице(К.лимит);
  console.log('лимит:', л.коды.join(' '), л.код);
  if (л.коды.slice(0, 6).some(к_ => к_ !== 200) || л.коды[6] !== 429 || л.код !== 'rate_limited') ошибки.push('лимит ключа не сработал на 7-м запросе');
  console.log(ошибки.length ? 'ошибки:\n  ' + ошибки.join('\n  ') : 'ошибок нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

if (РЕЖИМ === 'телеграм' || РЕЖИМ === 'телеграм31' || РЕЖИМ === 'телеграм32') {
  // Дополнение Дня 30: показ Telegram-бота в Telegram Web владельца (telegram-plan § 6, D-T14).
  // Вкладка открыта сразу на чате с ботом; скрипт пишет ТОЛЬКО в этот чат — перед каждой
  // отправкой сверяет заголовок чата. Сцена 6 — та же вкладка на /admin сервиса.
  const ПАУЗА = Number(process.env.PAUSE || 3000);
  const БОТ = process.env.BOT_TITLE || 'AIChallengeBot';
  const АДМИН = process.env.ADMIN_URL || '';
  const К = ключиСервиса;
  const замечания = [], метки = [];
  const подождать = мс => new Promise(р => setTimeout(р, мс));
  const должно = (условие, чего) => { if (!условие) замечания.push(чего); };
  const подпись = async (сцена, текст) => {
    await выполнить(`(() => {
      let п = document.getElementById('подпись-показа');
      if (!п) {
        п = document.createElement('div');
        п.id = 'подпись-показа';
        п.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;' +
          'background:#0f172a;color:#f8fafc;padding:10px 18px;font:16px/1.45 system-ui,sans-serif;' +
          'box-shadow:0 -2px 10px rgba(0,0,0,.25)';
        document.body.appendChild(п);
      }
      п.textContent = '';
      const б = document.createElement('b'); б.textContent = ${JSON.stringify(сцена)};
      п.append(б, ' — ' + ${JSON.stringify(текст)});
      return 'ок'; })()`);
    console.log(`\n=== ${сцена} — ${текст}`);
  };
  const чатБота = () => выполнить(`(() => { const з = document.querySelector('.chat-info .peer-title');
    return !!з && з.textContent === ${JSON.stringify(БОТ)} && !!document.querySelector('.input-message-input[contenteditable]'); })()`);
  // Новые сообщения бота — по номеру (data-mid растёт), а не по числу пузырей: Telegram Web
  // выгружает невидимые старые пузыри, число падает, и показ ждал ответ, который уже пришёл.
  const входящих = () => выполнить(`Math.max(0, ...[...document.querySelectorAll('.bubble.is-in')]
    .map(б => Number(б.dataset.mid) || 0))`);
  // Тексты сообщений бота, появившихся после «было»: ответ ищется среди всех новых — во время
  // ответа /doc приходит и ответ на /status (версия 7), и последний пузырь уже не тот.
  const входящиеПосле = (было) => выполнить(`[...document.querySelectorAll('.bubble.is-in')]
    .filter(б => (Number(б.dataset.mid) || 0) > ${Number(было) || 0})
    .map(б => (б.querySelector('.message') || {}).innerText || '')`);
  const скрытьСписок = () => выполнить(`(() => {
    if (!document.getElementById('показ-без-списка')) {
      const с = document.createElement('style');
      с.id = 'показ-без-списка';
      с.textContent = '#column-left { display: none !important; }';
      document.head.appendChild(с);
    }
    return 1; })()`);
  const списокСкрыт = () => выполнить(`(() => { const л = document.getElementById('column-left');
    return !л || getComputedStyle(л).display === 'none' || л.getBoundingClientRect().width === 0; })()`);
  const отправить = async (текст) => {
    await скрытьСписок();
    if (!(await списокСкрыт())) throw new Error('список чатов виден — отправка отменена');
    должно(await ждатьНаСтранице(`(() => { const з = document.querySelector('.chat-info .peer-title');
      return !!з && з.textContent === ${JSON.stringify(БОТ)}; })()`, 30000), 'открыт не чат с ботом — отправка отменена');
    if (!(await чатБота())) throw new Error('открыт не чат с ботом');
    const было = await входящих();
    await выполнить(`(() => { document.querySelector('.input-message-input[contenteditable]').focus(); return 1; })()`);
    await зов('Input.insertText', {text: текст});
    await подождать(400);
    await зов('Input.dispatchKeyEvent', {type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
    await зов('Input.dispatchKeyEvent', {type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13});
    return было;
  };
  // Ждать новый ответ бота с признаком (подпись ответа); пока ждём — подпись показывает ход.
  const ответБота = async (было, признак, сцена, что, мс = 1800000) => {
    const начало = Date.now();
    let прошлый = 0;
    while (Date.now() - начало < мс) {
      await подождать(1000);
      const новые = await входящиеПосле(было) || [];
      if (!новые.length) continue;
      const найден = новые.find(т_ => т_.includes(признак));
      if (найден) return найден;
      const т = новые[0];
      if (Date.now() - прошлый > 4000) {
        прошлый = Date.now();
        await подпись(сцена, т.startsWith('⏳') ? `${что}: ${т.split('\n')[0].replace('⏳ ', '')} (${Math.round((Date.now() - начало) / 1000)} с)`
                                              : `${что}: ответ дописывается правками — ${т.length} символов`);
      }
    }
    return '';
  };
  const подписьОтвета = т => (т.split('\n').find(с => с.startsWith('— loc-rag-docs')) || '').replace(/\d{1,2}:\d{2}$/, '').trim();

  должно(await ждатьНаСтранице(`!!document.querySelector('.chat-info .peer-title')`, 60000), 'Telegram Web не открыл чат с ботом');
  // Список чатов владельца в кадр не попадает: пока Telegram Web загружается, он показывает две
  // колонки, и в первой записи бота в кадре были имена и превью личных переписок. Левая колонка
  // скрывается стилем, а запись начинается только после подтверждения (файл READY → RECORDING).
  await скрытьСписок();
  должно(await списокСкрыт(), 'список чатов не скрыт — запись не начинается');
  if (!(await списокСкрыт())) { console.log('\n=== итог показа ===\nРАСХОЖДЕНИЯ:\n  список чатов не скрыт'); process.exit(0); }
  if (process.env.READY) {
    const fs = await import('node:fs');
    fs.writeFileSync(process.env.READY, '1');
    const конецОжидания = Date.now() + 60000;
    while (process.env.RECORDING && !fs.existsSync(process.env.RECORDING) && Date.now() < конецОжидания) await подождать(200);
  }
  await начатьЗапись();          // запись через CDP — только теперь, когда список чатов скрыт
  if (РЕЖИМ === 'телеграм32') {
    // День 32: уведомление владельцу от AI-ревью PR (эскалация человеку, лекция недели 7). Скрипт
    // ничего не отправляет — только показывает последние сообщения бота в чате с ним.
    if (!(await чатБота())) throw new Error('открыт не чат с ботом');
    await выполнить(`(() => { const п = document.querySelector('.bubbles .scrollable'); if (п) п.scrollTop = п.scrollHeight; return 1; })()`);
    await подпись('Сцена 6', 'Эскалация человеку: при красном ревью, сбое пайплайна или тревоге метрик Action пишет владельцу в Telegram — номер и ссылка PR, вердикт, счёт находок; без кода и текстов находок');
    const тексты = await выполнить(`[...document.querySelectorAll('.bubble.is-in')].slice(-6).map(б => (б.querySelector('.message') || {}).innerText || '')`);
    const уведомления = (тексты || []).filter(т => /AI-ревью PR #\d+/.test(т));
    console.log('уведомлений AI-ревью в последних сообщениях:', уведомления.length, уведомления.map(т => т.split('\n')[0]));
    должно(уведомления.length > 0, 'нет уведомления AI-ревью в чате с ботом');
    await подождать(ПАУЗА * 3);
    await остановитьЗапись();
    console.log('\n=== итог показа ===');
    console.log(замечания.length ? 'РАСХОЖДЕНИЯ:\n  ' + замечания.join('\n  ') : 'показ прошёл без расхождений');
    ws.close();
    process.exit(замечания.length ? 1 : 0);
  }

  if (РЕЖИМ === 'телеграм31') {
    // День 31: /help в Telegram — бот спрашивает ассистента разработчика через ручку сервиса.
    const признак = 'ток. · очередь';
    let было = await отправить('/new');
    await ответБота(было, 'заново', 'Сцена 1', '/new', 120000);
    было = await отправить('/help');
    const начало1 = Date.now();
    const справка = await ответБота(было, '/help вопрос', 'Сцена 1', 'справка', 120000);
    должно(/\/help вопрос/.test(справка), 'сцена 1: в справке нет /help вопрос');
    await подпись('Сцена 1', `Telegram Web, чат с ботом. /help без вопроса — справка сразу (${Math.round((Date.now() - начало1) / 1000)} с): новая строка «/help вопрос — ассистент разработчика: вопрос о проекте CRM»`);
    await подождать(ПАУЗА * 2.5);

    await подпись('Сцена 2', '«/help вопрос» — бот идёт в приватный сервис, ручка /v1/project/help: ассистент смотрит git и документацию проекта');
    let начало = Date.now();
    было = await отправить('/help Какие модели данных есть в приложении org?');
    const о2 = await ответБота(было, признак, 'Сцена 2', 'ассистент');
    метки.push({начало, конец: Date.now(), что: 'ассистент'});
    должно(/Unit/.test(о2) && /models\.py/.test(о2) && /ветка/.test(о2), 'сцена 2: нет моделей, models.py или ветки');
    await подпись('Сцена 2', `Ответ за ${Math.round((Date.now() - начало) / 1000)} с: модели из backend/apps/org/models.py, ветка git, источники — под маской ПД (точный профиль)`);
    await подождать(ПАУЗА * 3);

    await подпись('Сцена 3', 'Вопрос о git: модель вызывает инструменты MCP; /status во время ответа — отвечает сразу');
    начало = Date.now();
    было = await отправить('/help Что изменилось в последнем коммите проекта?');
    await подождать(4000);
    await отправить('/status');
    const статус = await ответБота(было, 'Сервис:', 'Сцена 3', '/status', 120000);
    должно(/Сервис:/.test(статус), 'сцена 3: /status не ответил во время ответа');
    const о3 = await ответБота(было, признак, 'Сцена 3', 'ассистент');
    метки.push({начало, конец: Date.now(), что: 'git'});
    должно(/git_(diff|log)/.test(о3), 'сцена 3: нет вызова git_diff / git_log');
    await подпись('Сцена 3', `Ответ за ${Math.round((Date.now() - начало) / 1000)} с: «Вызовы git (только чтение)» — ${(о3.match(/git_\w+\([^)]*\)/g) || []).slice(0, 3).join(', ')}`);
    await подождать(ПАУЗА * 3);
  } else {

  // --- сцена 1 ---------------------------------------------------------------------
  // История живого бота — в памяти его процесса: прерванный дубль оставил свои реплики, и вопрос
  // сцены 2 ушёл с ними (вход 173 вместо 24). Показ начинается с /new.
  let было = await отправить('/new');
  await ответБота(было, 'заново', 'Сцена 1', '/new', 120000);
  было = await отправить('/start');
  const справка = await ответБота(было, 'только владельцу', 'Сцена 1', 'справка', 120000);
  должно(/только владельцу/.test(справка) && /не ловит/.test(справка), 'сцена 1: нет справки бота');
  await подпись('Сцена 1', `Telegram Web, чат с ботом @AIChBot: бот на домашнем сервере — клиент приватного сервиса Дня 30 со своим ключом; отвечает только владельцу. /start — что умеет: вопрос, /new, /doc, /status`);
  await подождать(ПАУЗА * 2.5);

  // --- сцена 2 ---------------------------------------------------------------------
  await подпись('Сцена 2', 'Вопрос модели: сначала «⏳», затем сообщение бота дописывается правками — поток SSE сервиса');
  let начало = Date.now();
  было = await отправить('Что такое SLA? Ответь в двух предложениях.');
  const о2 = await ответБота(было, '— loc-rag-docs · вход', 'Сцена 2', 'чат');
  метки.push({начало, конец: Date.now(), что: 'чат'});
  должно(!!о2, 'сцена 2: нет ответа с подписью');
  await подпись('Сцена 2', `Ответ потоком · ${подписьОтвета(о2).replace('— ', '')}`);
  await подождать(ПАУЗА * 2.5);

  // --- сцена 3 ---------------------------------------------------------------------
  await подпись('Сцена 3', 'Уточнение без повтора вопроса — бот помнит разговор (история в памяти процесса бота)');
  начало = Date.now();
  было = await отправить('А короче — одним предложением?');
  const о3 = await ответБота(было, '— loc-rag-docs · вход', 'Сцена 3', 'уточнение');
  метки.push({начало, конец: Date.now(), что: 'уточнение'});
  const вход2 = Number((о2.match(/вход (\d+)/) || [])[1]), вход3 = Number((о3.match(/вход (\d+)/) || [])[1]);
  должно(вход3 > вход2, 'сцена 3: вход второго вопроса не больше первого — история не ушла');
  было = await отправить('/new');
  await ответБота(было, 'заново', 'Сцена 3', '/new', 60000);
  await подпись('Сцена 3', `Ответ по истории: вход ${вход3} токенов против ${вход2} у первого вопроса — ушли обе реплики. /new — «История очищена — начинаем заново»`);
  await подождать(ПАУЗА * 2.5);

  // --- сцена 4 ---------------------------------------------------------------------
  await подпись('Сцена 4', '/doc — ответ по документам организации: поиск и модель на этой машине, в Telegram — ответ, источник и цитата');
  начало = Date.now();
  было = await отправить('/doc До какого числа нужно оплачивать услуги связи по договору VPN с Ростелекомом?');
  // Пока модель отвечает — /status (ручная проверка владельца: ждал 7 минут; версия 7 — сразу).
  await ждатьНаСтранице(`[...document.querySelectorAll('.bubble.is-in')].some(б => (Number(б.dataset.mid) || 0) > ${Number(было) || 0})`, 120000);
  await подождать(ПАУЗА);
  const былоСтатус = await входящих();
  const началоСтатуса = Date.now();
  await отправить('/status');
  const статус = await ответБота(былоСтатус, 'Сервис:', 'Сцена 4', '/status', 60000);
  const секундСтатуса = Math.round((Date.now() - началоСтатуса) / 1000);
  должно(/бот отвечает/.test(статус) && секундСтатуса <= 30, `сцена 4: /status во время ответа — ${секундСтатуса} с, без «бот отвечает»`);
  await подпись('Сцена 4', `/status, пока модель ищет ответ по документам, — ответ за ${секундСтатуса} с: «Сейчас бот отвечает на предыдущее сообщение» (ручная проверка: раньше ждал 7 минут)`);
  const о4 = await ответБота(было, 'по документам · вход', 'Сцена 4', 'документы');
  метки.push({начало, конец: Date.now(), что: 'документы'});
  должно(/20 числа/.test(о4) && /✓/.test(о4), 'сцена 4: нет «20 числа» с подтверждённой цитатой');
  await подпись('Сцена 4', `Ответ за ${Math.round((метки.at(-1).конец - начало) / 1000)} с: «20 числа», источник и цитата «✓» — фильтр ПД ничего не нашёл`);
  await подождать(ПАУЗА * 2.5);

  // --- сцена 5 ---------------------------------------------------------------------
  await подпись('Сцена 5', 'Ответ, где в документе есть ФИО и телефон: Telegram — внешнее облако, поэтому перед отправкой — маска ПД');
  начало = Date.now();
  было = await отправить('/doc Кто контактное лицо абонента по договору с Ростелекомом на стационарную связь (50 номеров) и как с ним связаться?');
  const о5 = await ответБота(было, 'по документам · вход', 'Сцена 5', 'документы с ПД');
  метки.push({начало, конец: Date.now(), что: 'документы с ПД'});
  const скрыто = (о5.match(/Скрыто фильтром ПД: (\d+) \(([^)]*)\)/) || []);
  должно(о5.includes('[скрыто]') && скрыто[1], 'сцена 5: маска ПД не видна');
  await подпись('Сцена 5', `Скрыто фильтром ПД: ${скрыто[1] || '—'} (${скрыто[2] || '—'}) — в Telegram ушло «[скрыто]», под ответом сказано, чего фильтр не ловит`);
  await подождать(ПАУЗА * 3);

  // --- сцена 6 ---------------------------------------------------------------------
  if (АДМИН) {
    await выполнить(`(() => { location.href = ${JSON.stringify(АДМИН)}; return 1; })()`);
    await подождать(2500);
    await ждатьНаСтранице(`document.readyState === 'complete' && location.pathname === '/admin'`, 30000);
    await админВойти(К.админ);
    const карта = await ждатьНаСтранице(`(() => { const с = document.getElementById('телеграм-служба'), н = document.getElementById('телеграм-сеть'),
      ж = document.getElementById('телеграм-журнал'); return с && н && ж ? {служба: с.innerText, сеть: н.innerText, строк: ж.querySelectorAll('tr').length - 1} : null; })()`, 30000);
    await выполнить(`(() => { const у = document.getElementById('телеграм-карта'); if (у) у.scrollIntoView({behavior: 'smooth', block: 'start'}); return 1; })()`);
    должно(карта && карта.служба === 'active' && /api\.telegram\.org/.test(карта.сеть), 'сцена 6: карточка бота не active или сеть не api.telegram.org');
    await подпись('Сцена 6', `/admin, карточка «Telegram-бот»: служба day31-tg ${карта?.служба}; ${(карта?.сеть || '').replace(/\s+/g, ' ')}; в журнале бота — ${карта?.строк} событий без текстов`);
    await подождать(ПАУЗА * 3);
  }

  }   // сцены Дня 30

  if (process.env.MARKS) {
    const fs = await import('node:fs');
    fs.writeFileSync(process.env.MARKS, JSON.stringify({ожидания: метки}, null, 1));
  }
  await остановитьЗапись();
  console.log('\n=== итог показа ===');
  console.log(замечания.length ? 'РАСХОЖДЕНИЯ:\n  ' + замечания.join('\n  ') : 'показ прошёл без расхождений');
  if (ошибки.length) console.log(`(сообщений консоли страницы: ${ошибки.length} — Telegram Web, не наша страница)`);
  ws.close();
  process.exit(0);          // показ не проверка: код возврата всегда 0
}

if (РЕЖИМ === 'показ32') {
  // День 32: AI-ревью PR на GitHub — публичный просмотр без входа в аккаунт, кадры CDP. Сцены —
  // DEMO-CHROME.md: PR с настоящей доработкой (🟢), прогон Actions, PR с посеянными ошибками (🔴),
  // исправляющий коммит (FIX_CMD отправляет его) — тот же комментарий становится 🟢.
  const ПАУЗА = Number(process.env.PAUSE || 3000);
  const РЕПО = process.env.GH_REPO;
  const PR1 = process.env.PR_CLEAN, PR2 = process.env.PR_SEEDED;
  const замечания = [];
  const метки = [];
  const подождать = мс => new Promise(р => setTimeout(р, мс));
  const должно = (условие, чего) => { if (!условие) замечания.push(чего); };
  const подпись = async (сцена, текст) => {
    await выполнить(`(() => {
      let п = document.getElementById('подпись-показа');
      if (!п) {
        п = document.createElement('div');
        п.id = 'подпись-показа';
        п.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;' +
          'background:#0f172a;color:#f8fafc;padding:10px 18px;font:16px/1.45 ' +
          'system-ui,sans-serif;box-shadow:0 -2px 10px rgba(0,0,0,.25)';
        document.body.appendChild(п);
      }
      п.innerHTML = '<b>' + ${JSON.stringify(сцена)} + '</b> — ' + ${JSON.stringify(текст)};
      return 'ок'; })()`);
    console.log(`\n=== ${сцена} — ${текст}`);
  };
  const открыть = async (адрес) => {
    await выполнить(`(() => { location.href = ${JSON.stringify(адрес)}; return 'ок'; })()`);
    await подождать(2500);
    await ждатьНаСтранице(`document.readyState === 'complete'`, 30000);
    await подождать(1500);
  };
  const комментарий = () => выполнить(`(() => {
    const к = [...document.querySelectorAll('.markdown-body')].find(e => /AI-ревью:/.test(e.innerText));
    if (!к) return null;
    const з = к.querySelector('h2');
    return {заголовок: з ? з.innerText : '', текст: к.innerText.slice(0, 4000),
            находок: к.querySelectorAll('li').length}; })()`);
  const кЭлементу = (выражение, блок = 'start') => выполнить(`(() => { const е = ${выражение};
    if (е) е.scrollIntoView({behavior: 'smooth', block: ${JSON.stringify(блок)}}); return !!е; })()`);
  const кКомментарию = () => кЭлементу(`[...document.querySelectorAll('.markdown-body')].find(e => /AI-ревью:/.test(e.innerText))`);
  const кЗаголовку = (текст) => кЭлементу(`[...document.querySelectorAll('.markdown-body h3, .markdown-body summary')].find(e => e.innerText.includes(${JSON.stringify(текст)}))`);
  const прокрутить = (пикселей) => выполнить(`(() => { window.scrollBy({top: ${пикселей}, behavior: 'smooth'}); return 'ок'; })()`);
  // Анонимный API GitHub — 60 запросов в час: ожидание прогона их выбирало (второй дубль). Токен —
  // только из окружения процесса (GH_API_TOKEN), в аргументы и журнал не попадает.
  const заголовкиAPI = process.env.GH_API_TOKEN ? {Authorization: `Bearer ${process.env.GH_API_TOKEN}`} : {};
  const проверкиPR = async (номер) => {
    const pr = await (await fetch(`https://api.github.com/repos/${РЕПО}/pulls/${номер}`, {headers: заголовкиAPI})).json();
    const р = await (await fetch(`https://api.github.com/repos/${РЕПО}/commits/${pr.head.sha}/check-runs`, {headers: заголовкиAPI})).json();
    return {sha: pr.head.sha, прогоны: (р.check_runs || []).map(c => ({имя: c.name, статус: c.status, итог: c.conclusion, адрес: c.html_url}))};
  };

  // Сцена 1 — PR с настоящей доработкой.
  await открыть(`https://github.com/${РЕПО}/pull/${PR1}`);
  await подпись('Сцена 1', 'День 32: автоматическое AI-ревью кода. На каждый PR GitHub Actions запускает ассистента: diff и изменённые файлы — через git-сервер MCP, опора — документация и код базы PR (RAG), три ревьюера параллельно');
  await подождать(ПАУЗА * 2);
  await кКомментарию();
  const к1 = await комментарий();
  console.log('PR1:', к1 && к1.заголовок);
  должно(к1 && /🟢/.test(к1.заголовок), 'PR1: комментарий не 🟢');
  await подпись('Сцена 1', 'PR с настоящей доработкой (ссылка на журнал прогона в комментарии): 🟢 — критичных находок нет; рекомендации с опорой на документы и код проекта');
  await подождать(ПАУЗА * 2);
  await кЗаголовку('Рекомендации');
  await подождать(ПАУЗА * 2);
  await выполнить(`(() => { const s = [...document.querySelectorAll('.markdown-body summary')].find(e => e.innerText.includes('Как сделано ревью'));
    if (s) { s.click(); s.scrollIntoView({behavior: 'smooth', block: 'start'}); } return !!s; })()`);
  await подпись('Сцена 1', 'Как сделано ревью: файлы и строки diff, вызовы git-сервера MCP, фрагменты опоры (документы, код, места вызова), проверка находок кодом, время и токены каждого ревьюера');
  await подождать(ПАУЗА * 2.5);
  await прокрутить(500);
  await подождать(ПАУЗА);
  метки.push({что: 'сцена1', когда: Date.now()});

  // Сцена 2 — прогон Actions.
  const п1 = await проверкиPR(PR1);
  const прогон1 = (п1.прогоны[0] || {}).адрес;
  if (прогон1) {
    await открыть(прогон1);
    await подпись('Сцена 2', `Пайплайн — workflow ai-review.yml на облачном раннере: checkout, зависимости, индекс базы, AI-ревью; проверка PR: ${(п1.прогоны[0] || {}).итог}. Журнал виден владельцу, итог — в сводке задания`);
    await подождать(ПАУЗА * 2.5);
  } else должно(false, 'PR1: нет прогона Actions');

  // Сцена 3 — PR с посеянными ошибками.
  await открыть(`https://github.com/${РЕПО}/pull/${PR2}`);
  await подпись('Сцена 3', 'PR с посеянными ошибками: бот отдаёт ответ без маски ПД, веб зовёт модель напрямую мимо общего клиента, git-серверу «только на чтение» добавлен push');
  await подождать(ПАУЗА * 2);
  await кКомментарию();
  const к2 = await комментарий();
  console.log('PR2:', к2 && к2.заголовок);
  должно(к2 && /🔴/.test(к2.заголовок), 'PR2: комментарий не 🔴');
  await подпись('Сцена 3', 'Вердикт 🔴: подтверждённые критичные находки. Красит не модель, а код: файл из PR, строка в изменённом участке, цитата diff найдена');
  await подождать(ПАУЗА * 2);
  await кЗаголовку('Потенциальные баги');
  await подпись('Сцена 3', 'Потенциальные баги — с файлом и строкой, почему это ошибка, как исправить и опорой: фрагменты спек, ARCHITECTURE и места вызова из базы');
  await подождать(ПАУЗА * 3);
  await кЗаголовку('Архитектурные проблемы');
  await подпись('Сцена 3', 'Архитектурные проблемы — нарушения правил проекта: git только на чтение, вызов модели через общий клиент и офлайн-предохранитель');
  await подождать(ПАУЗА * 3);
  await кЗаголовку('Рекомендации');
  await подождать(ПАУЗА * 1.5);
  await выполнить(`(() => { window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'}); return 'ок'; })()`);
  await подпись('Сцена 3', 'Проверка PR красная — слияние решает человек; владельцу ушло уведомление в Telegram');
  await подождать(ПАУЗА * 2);
  метки.push({что: 'сцена3', когда: Date.now()});

  // Сцена 4 — исправление.
  if (process.env.FIX_CMD) {
    const {execSync} = await import('node:child_process');
    const было = (await проверкиPR(PR2)).sha;
    await подпись('Сцена 4', 'Исправляю: коммит возвращает маску ПД, убирает прямой вызов модели и push — Action запускается заново на новый коммит');
    execSync(process.env.FIX_CMD, {stdio: 'inherit'});
    const начало = Date.now();
    let п2 = null;
    while (Date.now() - начало < 600000) {
      await подождать(10000);
      п2 = await проверкиPR(PR2);
      const сек = Math.round((Date.now() - начало) / 1000);
      await подпись('Сцена 4', `Новый коммит ${п2.sha.slice(0, 7)}: AI-ревью идёт… ${сек} с`);
      if (п2.sha !== было && п2.прогоны.length && п2.прогоны.every(p => p.статус === 'completed')) break;
    }
    метки.push({что: 'ожидание', начало, конец: Date.now()});
    await открыть(`https://github.com/${РЕПО}/pull/${PR2}`);
    await кКомментарию();
    const к3 = await комментарий();
    console.log('PR2 после исправления:', к3 && к3.заголовок, п2 && п2.прогоны.map(p => p.итог));
    должно(к3 && /🟢/.test(к3.заголовок), 'PR2 после исправления: комментарий не 🟢');
    await подпись('Сцена 4', `Тот же комментарий обновлён: ${к3 ? к3.заголовок.replace('🤖 ', '') : '—'} — проверка PR ${п2 && п2.прогоны.every(p => p.итог === 'success') ? 'зелёная' : 'не зелёная'}`);
    await подождать(ПАУЗА * 2.5);
    await выполнить(`(() => { window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'}); return 'ок'; })()`);
    await подождать(ПАУЗА * 2);
  }
  await остановитьЗапись();
  // Для ускоренной копии — только ожидания (начало и конец): прогон Action после исправления.
  if (process.env.MARKS) (await import('node:fs')).writeFileSync(process.env.MARKS,
    JSON.stringify({ожидания: метки.filter(м => м.начало && м.конец)}, null, 1));
  console.log('\n=== итог показа ===');
  console.log(замечания.length ? 'расхождения: ' + замечания.join('; ') : 'показ прошёл без расхождений');
  ws.close();
  process.exit(замечания.length ? 1 : 0);
}

if (РЕЖИМ === 'показ31') {
  // День 31: ассистент разработчика — /help на странице мини-чата агента (стенд 5001). Проект —
  // CRM владельца, только чтение. Сцены — DEMO-CHROME.md; ожидания ответов — в метки (MARKS).
  const ПАУЗА = Number(process.env.PAUSE || 3000);
  const замечания = [];
  const метки = [];
  const подождать = мс => new Promise(р => setTimeout(р, мс));
  const должно = (условие, чего) => { if (!условие) замечания.push(чего); };
  const подпись = async (сцена, текст) => {
    await выполнить(`(() => {
      let п = document.getElementById('подпись-показа');
      if (!п) {
        п = document.createElement('div');
        п.id = 'подпись-показа';
        п.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;' +
          'background:#0f172a;color:#f8fafc;padding:10px 18px;font:16px/1.45 ' +
          'system-ui,sans-serif;box-shadow:0 -2px 10px rgba(0,0,0,.25)';
        document.body.appendChild(п);
        document.body.style.paddingBottom = '90px';
      }
      п.innerHTML = '<b>' + ${JSON.stringify(сцена)} + '</b> — ' + ${JSON.stringify(текст)};
      return 'ок'; })()`);
    console.log(`\n=== ${сцена} — ${текст}`);
  };
  const кКарточке = () => выполнить(`(() => {
    const все = document.querySelectorAll('#чат-лента .сообщение.помощь');
    const п = все[все.length - 1];
    if (п) п.scrollIntoView({behavior: 'smooth', block: 'start'});
    return !!п; })()`);
  // Вопрос сначала виден в поле ввода, потом отправка — зрителю видно, что спросили.
  const спросить = async (сцена, вопрос, что) => {
    await выполнить(`(() => { const п = document.getElementById('чат-реплика'); п.value = ${JSON.stringify(вопрос)};
      п.scrollIntoView({behavior: 'smooth', block: 'center'}); return 'ок'; })()`);
    await подпись(сцена, `Спрашиваю: ${вопрос}`);
    await подождать(ПАУЗА);
    const начало = Date.now();
    const было = (await последняяПомощь()).карточек;
    await выполнить(`document.getElementById('чат-отправить').click(), 'ок'`);
    let о = {};
    while (Date.now() - начало < 900000) {
      await подождать(1000);
      о = await последняяПомощь();
      if (о.карточек > было) break;
    }
    метки.push({начало, конец: Date.now(), что});
    await кКарточке();
    await подождать(800);
    return {...о, секунд: Math.round((Date.now() - начало) / 1000)};
  };
  const записатьМетки = async () => {
    if (!process.env.MARKS) return;
    const fs = await import('node:fs');
    fs.writeFileSync(process.env.MARKS, JSON.stringify({ожидания: метки}, null, 1));
  };

  // В кадре — одна вкладка: начальная about:blank, с которой стартовал Chrome, закрывается.
  for (const в of await (await fetch(`${CDP}/json/list`)).json())
    if (в.type === 'page' && в.id !== цель.id) await fetch(`${CDP}/json/close/${в.id}`);

  // --- сцена 1 -----------------------------------------------------------------
  await наСтраницеЧата('/chat');
  await открытьСессию('показ-31');
  await новыйРазговорНаСтранице();
  await подпись('Сцена 1', 'День 31: ассистент разработчика. Проект — CRM владельца (портал ИТ-отдела, только чтение). RAG по документации: README, docs, specs, схемы данных (models.py) и API (urls.py); git — через свой MCP-сервер только на чтение; команда /help');
  await подождать(ПАУЗА * 2.5);

  // --- сцена 2 -----------------------------------------------------------------
  await выполнить(`(() => { const п = document.getElementById('чат-реплика'); п.value = '/help'; return 'ок'; })()`);
  await подпись('Сцена 2', '/help без вопроса — справка по командам чата');
  await подождать(ПАУЗА);
  await выполнить(`document.getElementById('чат-отправить').click(), 'ок'`);
  let справка = '';
  for (let i = 0; i < 30 && !справка; i++) {
    await подождать(500);
    справка = await выполнить(`(document.querySelector('#чат-ход .справка-помощи') || {}).innerText || ''`);
  }
  должно(/\/help вопрос/.test(справка), 'сцена 2: нет справки');
  await выполнить(`(() => { const х = document.getElementById('чат-ход'); х.scrollIntoView({behavior: 'smooth', block: 'center'}); return 'ок'; })()`);
  await подождать(ПАУЗА * 2);

  // --- сцена 3 -----------------------------------------------------------------
  const о3 = await спросить('Сцена 3', '/help Как устроен проект и где что лежит?', 'структура');
  должно(о3.источники && о3.источники.length && /ветка/.test(о3.понято || ''), 'сцена 3: нет источников или ветки');
  await подпись('Сцена 3', `Ответ за ${о3.секунд} с: по документации проекта — источники ${(о3.источники || []).map(и => и.replace(/^\[\d+\] /, '').split(' · ')[0]).filter((и, i, все) => все.indexOf(и) === i).slice(0, 3).join(', ')}; цитаты проверены кодом (✓ дословно); ветка git — через MCP`);
  await подождать(ПАУЗА * 3);

  // --- сцена 4 -----------------------------------------------------------------
  const о4 = await спросить('Сцена 4', '/help Какие модели данных есть в приложении org?', 'схема данных');
  должно((о4.источники || []).some(и => /models\.py/.test(и)) && /Unit/.test(о4.текст || ''), 'сцена 4: нет models.py');
  await подпись('Сцена 4', `Схема данных — из кода проекта: ${(о4.источники || []).filter(и => /models\.py/.test(и)).slice(0, 1).map(и => и.replace(/^\[\d+\] /, '').split(' · ')[0])} — ответ за ${о4.секунд} с`);
  await подождать(ПАУЗА * 3);

  // --- сцена 5 -----------------------------------------------------------------
  const о5 = await спросить('Сцена 5', '/help Какие маршруты (URL) есть у сервис-деска?', 'API');
  должно((о5.источники || []).some(и => /urls\.py/.test(и)), 'сцена 5: нет urls.py');
  await подпись('Сцена 5', `Описание API — маршруты Django из urls.py, ответ за ${о5.секунд} с`);
  await подождать(ПАУЗА * 3);

  // --- сцена 6 -----------------------------------------------------------------
  const о6 = await спросить('Сцена 6', '/help На какой ветке проект и что менялось в последних 3 коммитах?', 'git');
  должно((о6.вызовы || []).some(в => /git_log/.test(в)), 'сцена 6: нет вызова git_log');
  await подпись('Сцена 6', `Контекст проекта — git через MCP: модель сама вызвала ${(о6.вызовы || []).map(в => (в.match(/git_\w+/) || [''])[0]).filter(Boolean).join(', ')}; ветка и статус ассистент берёт всегда`);
  await подождать(ПАУЗА * 3);

  // --- сцена 7 -----------------------------------------------------------------
  const о7 = await спросить('Сцена 7', '/help Сколько принципов в конституции проекта и на какие разделы они делятся?', 'файл целиком');
  должно((о7.вызовы || []).some(в => /git_show/.test(в)) && /16/.test(о7.текст || ''), 'сцена 7: нет git_show или «16»');
  await подпись('Сцена 7', 'Фрагментов мало, чтобы посчитать, — ассистент прочитал файл целиком инструментом git_show (из коммита, только чтение) и посчитал: 16 принципов');
  await подождать(ПАУЗА * 3);

  // --- сцена 8 -----------------------------------------------------------------
  const о8 = await спросить('Сцена 8', '/help Какой брокер Kafka использует портал и на каком порту?', 'ловушка');
  должно(о8.неЗнаю || /не используется|нет/i.test(о8.текст || ''), 'сцена 8: ловушка без «не знаю»');
  await подпись('Сцена 8', 'Ловушка: Kafka в проекте нет — ассистент не выдумывает порт');
  await подождать(ПАУЗА * 2.5);

  // --- сцена 9 -----------------------------------------------------------------
  const о9 = await спросить('Сцена 9', '/help Закоммить мои изменения и отправь их в origin', 'опасная');
  должно(/не могу|нельзя|только (на )?чтение|нет такого инструмента|такого инструмента нет/i.test(о9.текст || ''), 'сцена 9: нет отказа');
  await подпись('Сцена 9', 'Опасная операция (лекция недели 7): меняющих инструментов у ассистента нет вовсе — git только на чтение, отказ');
  await подождать(ПАУЗА * 3);

  await записатьМетки();
  await остановитьЗапись();
  console.log('\n=== итог показа ===');
  console.log(замечания.length ? 'РАСХОЖДЕНИЯ:\n  ' + замечания.join('\n  ')
                               : 'показ прошёл без расхождений');
  if (ошибки.length) console.log('ошибки страницы:\n  ' + ошибки.join('\n  '));
  ws.close();
  process.exit(0);          // показ не проверка: код возврата всегда 0
}

if (РЕЖИМ === 'показ') {
  // День 30: локальная LLM как приватный сервис — по живой службе day31-llm. Живые генерации на
  // CPU идут минутами; начало и конец каждого ожидания — в файл меток (MARKS), по ним
  // demo_speedup.py собирает копию, где ускорено только ожидание.
  const ПАУЗА = Number(process.env.PAUSE || 3000);
  const замечания = [];
  const метки = [];
  const подождать = мс => new Promise(р => setTimeout(р, мс));
  const должно = (условие, чего) => { if (!условие) замечания.push(чего); };
  const подпись = async (сцена, текст) => {
    await выполнить(`(() => {
      let п = document.getElementById('подпись-показа');
      if (!п) {
        п = document.createElement('div');
        п.id = 'подпись-показа';
        п.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;' +
          'background:#0f172a;color:#f8fafc;padding:10px 18px;font:16px/1.45 ' +
          'system-ui,sans-serif;box-shadow:0 -2px 10px rgba(0,0,0,.25)';
        document.body.appendChild(п);
        document.body.style.paddingBottom = '70px';
      }
      п.innerHTML = '<b>' + ${JSON.stringify(сцена)} + '</b> — ' + ${JSON.stringify(текст)};
      return 'ок'; })()`);
    console.log(`\n=== ${сцена} — ${текст}`);
  };
  const к = async (селектор, блок = 'start') => выполнить(`(() => {
    const у = document.querySelector(${JSON.stringify(селектор)});
    if (у) у.scrollIntoView({behavior: 'smooth', block: ${JSON.stringify(блок)}});
    return !!у; })()`);
  const записатьМетки = async () => {
    if (!process.env.MARKS) return;
    const fs = await import('node:fs');
    fs.writeFileSync(process.env.MARKS, JSON.stringify({ожидания: метки}, null, 1));
  };
  const ч = (в, з = 1) => в == null ? '—' : String(Number(в).toFixed(з)).replace('.', ',');
  const К = ключиСервиса;
  должно(К.админ && К.чат && К.документы && К.лимит, 'нет ключей показа (SERVICE_KEYS)');

  // --- сцена 1 -----------------------------------------------------------------
  await админВойти(К.админ);
  const с = await состояниеСервиса(К.админ) || {};
  const сл = с.service || {}, л = сл.лимиты || {}, адрес = (с.cert || {}).адрес_в_сети;
  должно((с.unit || {}).состояние === 'active', 'сцена 1: служба day31-llm не active');
  await к('#служба');
  await подпись('Сцена 1', `День 30: локальная LLM как приватный сервис. Служба systemd «day31-llm» — ${(с.unit || {}).состояние}, https://${адрес}:${сл.порт} в домашней сети, свой сертификат; Ollama слушает только 127.0.0.1 — наружу выходит только шлюз`);
  await подождать(ПАУЗА * 2);

  // --- сцена 2 -----------------------------------------------------------------
  await к('#лимиты', 'center');
  await подпись('Сцена 2', `Модель ${сл.модель}. Ключи API с правами (чат, документы, админ) — на диске только хэши; подсети — домашняя сеть и Docker; лимиты: ${л.запросов_в_минуту} запросов в минуту на ключ, очередь 1 + ${л.очередь}, вход ≤ ${л.вход_токенов} токенов, ответ ≤ ${л.ответ_токенов}`);
  await подождать(ПАУЗА * 2.5);

  // --- сцена 3 -----------------------------------------------------------------
  await перейти('/chat');
  await чатКлюч(К.чат, 'чат');
  await подпись('Сцена 3', 'Чат: OpenAI-совместимый /v1/chat/completions с потоком — ключ введён как пароль, ответ идёт кусками SSE');
  const начало3 = Date.now();
  const о3 = await спроситьВЧате(ВОПРОС_ЧАТА, 900000, async ход => подпись('Сцена 3', `Чат: ${ход}`));
  метки.push({начало: начало3, конец: Date.now(), что: 'чат'});
  должно(о3.ответ && о3.подписи.some(п => /вход \d+ · ответ \d+ токенов/.test(п)), 'сцена 3: нет ответа или подписи с токенами');
  await подпись('Сцена 3', `Ответ за ${о3.секунд} с · ${(о3.подписи[0] || '').replace(/^loc-rag-docs · /, '')}`);
  await подождать(ПАУЗА * 2);

  // --- сцена 4 -----------------------------------------------------------------
  await чатКлюч(К.документы, 'документы');
  await подпись('Сцена 4', 'По документам: /v1/documents/answer — поиск и ответ на этой машине; только для ключа с правом «документы»');
  const начало4 = Date.now();
  const о4 = await спроситьВЧате(ВОПРОС_ДОКУМЕНТЫ, 900000);
  метки.push({начало: начало4, конец: Date.now(), что: 'документы'});
  должно(/\b20\b/.test(о4.ответ) && о4.цитаты.some(ц => /✓ дословно/.test(ц)), 'сцена 4: нет «20 числа» с дословной цитатой');
  await подпись('Сцена 4', `Ответ за ${о4.секунд} с: «${о4.ответ.slice(0, 70)}» — источник и цитата «✓ дословно»`);
  await подождать(ПАУЗА * 2);
  await чатКлюч(К.чат, 'документы');
  const о4б = await спроситьВЧате(ВОПРОС_ДОКУМЕНТЫ, 60000);
  должно(/HTTP 403/.test(о4б.отказ), 'сцена 4: ключ «чат» прошёл к документам');
  await подпись('Сцена 4', `Тот же вопрос ключом без права: ${о4б.отказ.slice(0, 100)}`);
  await подождать(ПАУЗА * 1.5);

  // --- сцена 5 -----------------------------------------------------------------
  await чатКлюч(К.чат, 'чат');
  const о5 = await спроситьВЧате('Проверка предела контекста. ' + 'слово '.repeat(4200), 60000);
  должно(/HTTP 413/.test(о5.отказ) && /context_length_exceeded/.test(о5.отказ), 'сцена 5: длинный вход не отклонён 413');
  await подпись('Сцена 5', `Предел контекста: ${о5.отказ.replace(/^HTTP 413 · context_length_exceeded: /, '').slice(0, 120)} — 413 до вызова модели`);
  await подождать(ПАУЗА * 2);
  const л5 = await лимитНаСтранице(К.лимит);
  должно(л5 && л5.коды.slice(0, 6).every(к_ => к_ === 200) && л5.коды[6] === 429 && л5.код === 'rate_limited',
    'сцена 5: лимит ключа не сработал на 7-м запросе');
  await подпись('Сцена 5', `Лимит ключа: семь запросов подряд — ${л5.коды.join(' ')}; седьмой — 429 rate_limited с Retry-After`);
  await подождать(ПАУЗА * 2);

  // --- сцена 6 -----------------------------------------------------------------
  await перейти('/admin');
  await админВойти(К.админ);
  const с6 = await состояниеСервиса(К.админ) || {};
  const адреса = (с6.clients || []).map(к_ => к_.адрес);
  const докер = адреса.find(а => а.startsWith('172.17.'));
  const телефон = адреса.find(а => /^192\.168\./.test(а) && а !== адрес);
  должно(докер, 'сцена 6: в журнале нет клиента из Docker');
  должно(телефон, 'сцена 6: в журнале нет запроса с телефона');
  await к('#клиенты-карта');
  await подпись('Сцена 6', `Доступ по сети: клиенты по журналу — ${телефон || '—'} (телефон в Wi-Fi), ${докер || '—'} (контейнер Docker), ${адрес} (сам ноутбук); в журнале только метаданные, без текстов`);
  await подождать(ПАУЗА * 2.5);

  // --- сцена 7 -----------------------------------------------------------------
  await к('#нагрузка-карта');
  await подпись('Сцена 7', 'Нагрузка вживую: 8 клиентов из контейнера Docker одновременно — одна генерация идёт, 4 ждут, остальным 429 queue_full с Retry-After, и они повторяют');
  const начало7 = Date.now();
  await выполнить(`(() => { document.getElementById('нагрузка-8').click(); return 'ок'; })()`);
  let начато = false, максЖдут = 0, отказаноДо = (с6.queue || {}).отказано || 0, о7 = {};
  while (Date.now() - начало7 < 3600000) {
    await подождать(2000);
    о7 = await состояниеСервиса(К.админ) || {};
    const о = о7.queue || {};
    if ((о7.load || {}).идёт) начато = true;
    максЖдут = Math.max(максЖдут, о.ждут || 0);
    await подпись('Сцена 7', `Нагрузка из Docker: идёт ${о.идёт} · ждут ${о.ждут} из ${о.мест_в_очереди} · отказано (429 queue_full) ${(о.отказано || 0) - отказаноДо} · обслужено ${о.обслужено}`);
    if (начато && !(о7.load || {}).идёт) break;
  }
  метки.push({начало: начало7, конец: Date.now(), что: 'нагрузка'});
  await к('#нагрузка');
  const п7 = ((о7.load || {}).последний || {}).ступени?.[0] || {};
  должно(п7.клиентов === 8 && п7.успешно === 8 && п7.ошибок === 0, 'сцена 7: не все 8 ответов получены или есть ошибки');
  должно(максЖдут === (л.очередь || 4) && (п7.отказов_429 || 0) > 0, 'сцена 7: очередь не заполнялась или не было отказов 429');
  // Подпись — по данным: «все прошли при повторе» только когда так и есть (дубль 2 Дня 30
  // показал эти слова при «ошибок 1»).
  const повтор7 = !п7.отказов_429 ? 'отказов не было'
    : п7.ошибок ? `не дождались ответа ${п7.ошибок}: ${(п7.ошибки || []).join(', ')}` : 'все прошли при повторе';
  await подпись('Сцена 7', `Итог: ${п7.успешно} из ${п7.запросов} ответов, ошибок ${п7.ошибок}; отказов 429 — ${п7.отказов_429 || 0}, ${повтор7}; задержка p50 ${ч(п7.задержка_p50_с)} с, p95 ${ч(п7.задержка_p95_с)} с`);
  await подождать(ПАУЗА * 2.5);

  // --- сцена 8 -----------------------------------------------------------------
  const полный = (о7.load || {}).полный || {};
  const ст = полный.ступени || [];
  должно(ст.length === 4 && полный.всего_ошибок === 0, 'сцена 8: нет полного прогона 1→8 без ошибок');
  await подпись('Сцена 8', `Ступени ${ст.map(с_ => с_.клиентов).join(' → ')} (прогон ${полный.метка}): ошибок ${полный.всего_ошибок}; задержка p50 ${ст.map(с_ => ч(с_.задержка_p50_с, 0)).join(' → ')} с — растёт с очередью, а сервис не падает; пик ОЗУ раннера ${ч((ст[ст.length - 1]?.ресурсы || {}).пик_мб, 0)} МБ`);
  await подождать(ПАУЗА * 3);

  await записатьМетки();
  await остановитьЗапись();
  console.log('\n=== итог показа ===');
  console.log(замечания.length ? 'РАСХОЖДЕНИЯ:\n  ' + замечания.join('\n  ')
                               : 'показ прошёл без расхождений');
  if (ошибки.length) console.log('ошибки страницы:\n  ' + ошибки.join('\n  '));
  ws.close();
  process.exit(0);          // показ не проверка: код возврата всегда 0
}

if (РЕЖИМ === 'переходы') {
  // Отдельный ход: здесь проверяется жизненный цикл задачи, а не сценарий.
  // Модель зовётся один раз — на составление плана, без которого нечего
  // утверждать, а значит, и нечего открывать.
  const имя = 'ворота-' + Date.now().toString().slice(-6);
  await выполнить(`(() => {
    document.getElementById('новая-задача').value = ${JSON.stringify(имя)};
    document.getElementById('название').value = 'проверка ворот из браузера';
    document.getElementById('создать').click(); return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 2500));

  const кнопки = await выполнить(`Array.from(
    document.querySelectorAll('#переходы button')).map(к => к.textContent.trim())`);
  console.log('кнопки переходов:', кнопки);
  const запертых = (кнопки || []).filter(т => т.includes('🔒')).length;
  console.log('запертых кнопок:', запертых);
  if (!кнопки || кнопки.length < 3) ошибки.push('на странице нет кнопок переходов');
  if (!запертых) ошибки.push('все переходы показаны открытыми — ворота не видны');

  // Нажимаем запертый переход: страница должна показать разбор отказа.
  await выполнить(`(() => {
    const к = Array.from(document.querySelectorAll('#переходы button'))
      .find(к => к.textContent.includes('🔒'));
    if (к) к.click(); return к ? к.textContent : 'нет'; })()`);
  await new Promise(р => setTimeout(р, 2000));
  const отказов = await выполнить(`document.querySelectorAll('#чат .отказ').length`);
  const текстОтказа = await выполнить(
    `(document.querySelector('#чат .отказ') || {}).innerText || ''`);
  console.log('отказов в чате:', отказов);
  console.log('первые строки отказа:\n' + (текстОтказа || '(пусто)').split('\n').slice(0, 6).join('\n'));
  if (!отказов) ошибки.push('нажатие запертого перехода не дало отказа');

  console.log('\nсоставляю план (один вызов модели)…');
  await выполнить(`document.getElementById('план').click(); 'ок'`);
  const началоПлана = Date.now();
  while (Date.now() - началоПлана < 180000) {
    await new Promise(р => setTimeout(р, 3000));
    const занято = await выполнить(`document.getElementById('отправить').disabled`);
    if (!занято && Date.now() - началоПлана > 6000) break;
  }
  const доУтверждения = await выполнить(`document.getElementById('ворота').innerText`);
  console.log('\n=== ворота до утверждения ===\n' + (доУтверждения || '(пусто)'));

  await выполнить(`document.getElementById('утвердить-план').click(); 'ок'`);
  await new Promise(р => setTimeout(р, 2500));
  const послеУтверждения = await выполнить(`document.getElementById('ворота').innerText`);
  console.log('\n=== ворота после утверждения ===\n' + (послеУтверждения || '(пусто)'));

  const открыт = await выполнить(`(() => {
    const к = Array.from(document.querySelectorAll('#переходы button'))
      .find(к => к.textContent.includes('исполнение'));
    return к ? к.textContent.trim() : 'нет кнопки'; })()`);
  console.log('кнопка исполнения после утверждения:', открыт);
  if (открыт.includes('🔒')) ошибки.push('после утверждения плана переход остался закрытым');

  // Шаги задачи, заведённой руками, закрывает кнопка «Шаг сделан»: без неё
  // условие «шаги доведены» из браузера не выполнить вовсе.
  await выполнить(`(() => {
    const к = Array.from(document.querySelectorAll('#переходы button'))
      .find(к => к.textContent.includes('исполнение'));
    if (к) к.click(); return 'ок'; })()`);
  await new Promise(р => setTimeout(р, 2000));
  const шаговДо = await выполнить(
    `document.querySelectorAll('.лента-шагов .готов').length`);
  await выполнить(`document.getElementById('шаг-значение').value = 'сделано из браузера';
                   document.getElementById('закрыть-шаг').click(); 'ок'`);
  await new Promise(р => setTimeout(р, 2000));
  const шаговПосле = await выполнить(
    `document.querySelectorAll('.лента-шагов .готов').length`);
  console.log(`шагов закрыто: было ${шаговДо}, стало ${шаговПосле}`);
  if (шаговПосле <= шаговДо) ошибки.push('кнопка «Шаг сделан» не закрыла шаг');

  const сбоиПереходов = await выполнить(`document.querySelectorAll('#чат .ошибка').length`);
  console.log('\n=== итог ===');
  console.log('сообщений об ошибке в чате:', сбоиПереходов);
  console.log('исключений JS:', ошибки.length ? ошибки : 'нет');
  ws.close();
  process.exit(ошибки.length ? 1 : 0);
}

if (РЕЖИМ !== 'вопрос') {
  console.log(`неизвестный режим «${РЕЖИМ}»`);
  ws.close();
  process.exit(2);
}

await выполнить(`document.getElementById('ввод').value = ${JSON.stringify(ЗАПРОС)};
                 document.getElementById('отправить').click(); 'пуск'`);

const начало = Date.now();
let прошлое = '';
while (Date.now() - начало < ПРЕДЕЛ_МС) {
  await new Promise(р => setTimeout(р, 3000));
  const чат = await выполнить(`document.getElementById('чат').innerText`);
  const новое = (чат || '').slice(прошлое.length);
  if (новое.trim()) {
    for (const строка of новое.split('\n').filter(с => с.trim()).slice(0, 3)) {
      console.log(`[${((Date.now() - начало) / 1000).toFixed(0)} с] ${строка.slice(0, 110)}`);
    }
    прошлое = чат;
  }
  const занято = await выполнить(`document.getElementById('отправить').disabled`);
  if (!занято && Date.now() - начало > 8000) break;
}

const чат = await выполнить(`document.getElementById('чат').innerText`);
const отвечено = await выполнить(`document.querySelectorAll('#чат .от-агента').length`);
const сбои = await выполнить(`document.querySelectorAll('#чат .ошибка').length`);

console.log('\n=== итог ===');
console.log('символов в чате:', (чат || '').length);
console.log('ответов агента:', отвечено);
console.log('сообщений об ошибке в чате:', сбои);
console.log('исключений JS:', ошибки.length ? ошибки : 'нет');

ws.close();
// Ненулевой код — чтобы прогон годился и для проверки перед сдачей.
process.exit(ошибки.length || сбои || !отвечено ? 1 : 0);
