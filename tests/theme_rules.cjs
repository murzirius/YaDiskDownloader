const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../disk_downloader/static/upgrade.js'),'utf8').split('\ntry{')[0];
const context=vm.createContext({});
vm.runInContext(source+'\nglobalThis.validate=validateTheme;globalThis.themes=baseThemes;',context);
const theme=()=>JSON.parse(JSON.stringify(context.themes[0]));
test('built-in themes have readable colours',()=>{
  for(const value of context.themes)assert.equal(context.validate(value).name,value.name);
});
test('theme rejects executable styles, URLs and extra properties',()=>{
  for(const color of ['url(https://example.invalid/file)','red;display:none','expression(alert(1))',{},null]){
    const value=theme();value.colors.bg=color;assert.throws(()=>context.validate(value));
  }
  const value=theme();value.script='alert(1)';assert.throws(()=>context.validate(value));
  assert.throws(()=>context.validate(JSON.parse('{"name":"x","colors":{},"__proto__":{}}')));
});
test('theme rejects unreadable text and incomplete colour maps',()=>{
  const value=theme();value.colors.text=value.colors.panel;assert.throws(()=>context.validate(value));
  const incomplete=theme();delete incomplete.colors.line;assert.throws(()=>context.validate(incomplete));
});
