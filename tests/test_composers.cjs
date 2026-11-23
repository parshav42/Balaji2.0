const assert = require('node:assert/strict');
const fs = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

const context = vm.createContext({
  el: () => ({addEventListener() {}}),
  request: () => new Promise(() => {}),
  window: {addEventListener() {}},
  document: {addEventListener() {}},
  navigator: {},
  setInterval() {}
});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname, '../web/control.js'), 'utf8'), context);
const {composeTarget} = context;

test('uses fixed compose targets and encodes message parameters', () => {
  assert.equal(composeTarget('sms', '+15555550100', 'Go & check?', false),
    'sms:+15555550100?body=Go%20%26%20check%3F');
  assert.equal(composeTarget('sms', '+15555550100', 'Hi', true), 'sms:+15555550100');
  assert.equal(composeTarget('whatsapp', '+15555550100', 'Test', false), 'https://wa.me/15555550100?text=Test');
});

test('rejects missing recipients, URL injection, and unsupported apps', () => {
  assert.throws(() => composeTarget('sms', '', 'Hello', false), /international number/);
  assert.throws(() => composeTarget('sms', '+15555550100?body=secret', 'Hello', false), /international number/);
  assert.throws(() => composeTarget('file', '+15555550100', 'Hello', false), /Choose SMS or WhatsApp/);
});
