const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(__dirname + '/../src/renderer/app.js', 'utf8');
function get(name, next) { return code.slice(code.indexOf('function ' + name + '('), code.indexOf(next, code.indexOf('function ' + name + '('))); }
const fields = {
 'gen-duration': {value:'13', removeAttribute(name) {delete this[name];}},
 'gen-duration-hint': {}, 'gen-text': {value:'A person walks forward slowly with relaxed arms and a steady natural stride.'},
 'gen-model': {value:'exact_english', options:[{value:'exact_english'}]}, 'gen-count': {value:'1'}
};
const context = vm.createContext({ S:{gen:{durationMax:null}}, $: id => fields[id], updateChecks(){} });
vm.runInContext(get('syncGeneration', 'async function loadModels'), context);
vm.runInContext(get('promptChecks', 'function updateChecks'), context);
const sync = generation => context.syncGeneration(generation);
const valid = duration => {fields['gen-duration'].value = String(duration); return context.promptChecks().ok;};
sync({duration_max_seconds:120});
assert.equal(fields['gen-duration'].max, '120');
assert.equal(valid(13), true); assert.equal(valid(120), true); assert.equal(valid(121), false);
assert.equal(valid(0), false); assert.equal(valid(1.5), false); assert.equal(valid(Infinity), false);
fields['gen-duration'].value = '120'; sync({duration_max_seconds:12});
assert.equal(fields['gen-duration'].value, '120', 'model change must not silently shorten motion');
assert.equal(valid(120), false); assert.equal(valid(12), true);
sync(undefined); assert.equal(fields['gen-duration'].max, undefined); assert.equal(valid(30), true);
assert.equal(valid(Number.MAX_SAFE_INTEGER + 1), false);
console.log('PASS: 13/120 seconds, bounds, invalid duration, model changes, legacy server fallback');
