// SLC-001..007; STG-002/004; ACC-011/012. All API requests are intercepted.
async page => {
  const check=(value,message)=>{if(!value)throw Error(message);};
  const base={
    library:{library_id:'test-library',library_root:'/tmp/pal-ui-library',target_clis:['claude-code','codex']},
    config_root:'/tmp/pal-ui-config',default_binding:{matches_selected:true,configured:true},
    units:[
      {unit_id:'alpha',description:'整理项目报告',development_revision_id:'alpha-new',production_revision_id:'alpha-old',mounted_revision_id:'alpha-old',mount_enabled:true},
      {unit_id:'beta',description:'待发布的新 Skill',development_revision_id:'beta-new',production_revision_id:null,mounted_revision_id:null,mount_enabled:true},
      {unit_id:'gamma',description:'保持现状',development_revision_id:'gamma-old',production_revision_id:'gamma-old',mounted_revision_id:'gamma-old',mount_enabled:true},
    ],
    mount:{version_id:'mount-one',sync_required:false,recovery_required:false,targets:[]},
    production:{active_version_id:'production-one',members:[]},
  };
  let status=JSON.parse(JSON.stringify(base)),generation=0,writeFailure=false,readFailure=false;
  const posts=[],errors=[],nativeDialogs=[],passed=[];
  const onError=e=>errors.push(e.message),onDialog=d=>{nativeDialogs.push(d.type());void d.dismiss();};
  page.on('pageerror',onError);page.on('dialog',onDialog);
  function recalc(){
    status.units.forEach(u=>{u.current_revision_id=u.development_revision_id;u.active_revision_id=u.production_revision_id;});
    status.production.members=status.units.filter(u=>u.production_revision_id).map(u=>({unit_id:u.unit_id,revision_id:u.production_revision_id}));
    status.mount.sync_required=status.units.some(u=>u.pending_deletion || (u.mount_enabled===false ? u.mounted_revision_id!==null : u.production_revision_id!==u.mounted_revision_id));
  }
  recalc();
  const route=async route=>{
    const req=route.request(),path=req.url().split('/').slice(3).join('/').split('?')[0].replace(/^/,'/');
    const reply=(body,code=200)=>route.fulfill({status:code,contentType:'application/json',body:JSON.stringify(body)});
    if(req.method()==='GET'){
      if(readFailure)return reply({error:'测试：状态读取失败'},409);
      if(path==='/api/monitor')return reply({checked_at:new Date().toISOString(),production_version_id:status.production.active_version_id,mounted_version_id:status.mount.version_id,targets:status.library.target_clis.map(cli_id=>({cli_id,creation:{state:'healthy'},mount:{state:'healthy'}}))});
      if(path.startsWith('/api/skills/')){
        const u=status.units.find(u=>u.unit_id===path.split('/').pop());
        const source=revision_id=>revision_id ? {revision_id,artifacts:[{artifact_id:revision_id,profile_id:'basic',canonical_path:'skills/alpha/SKILL.md',files:[],description:'整理项目报告',covered_clis:['claude-code','codex'],instructions:revision_id+' <img src=x onerror=alert(1)>'}]} : null;
        return reply({unit_id:u.unit_id,development:source(u.development_revision_id),production:source(u.production_revision_id),mounted:source(u.mounted_revision_id)});
      }
      return reply(status);
    }
    const body=req.postDataJSON();posts.push({path,body});
    check(req.headers()['x-pal-action']==='confirm','Write requests require confirmation envelope');
    if(writeFailure)return reply({error:'测试：基线变化，请重新确认'},409);
    if(path==='/api/skill-actions/preview'){
      const u=status.units.find(u=>u.unit_id===body.unit_id);
      return reply({...u,action:body.action,token:'reviewed-'+generation});
    }
    if(path==='/api/skill-actions/execute'){
      check(body.token==='reviewed-'+generation,'Execution binds preview');
      const u=status.units.find(u=>u.unit_id===body.unit_id);
      if(body.action==='publish'){u.production_revision_id=u.development_revision_id;status.production.active_version_id='production-'+(++generation);}
      if(body.action==='mount'){u.mounted_revision_id=u.production_revision_id;u.mount_enabled=true;status.mount.version_id='mount-'+(++generation);}
      if(body.action==='unmount'){u.mounted_revision_id=null;u.mount_enabled=false;if(u.pending_deletion)status.units=status.units.filter(x=>x!==u);status.mount.version_id='mount-'+(++generation);}
      if(body.action==='discard')u.development_revision_id=u.production_revision_id;
      if(body.action==='delete'){u.development_revision_id=null;u.production_revision_id=null;u.mount_enabled=false;u.pending_deletion=!!u.mounted_revision_id;if(!u.pending_deletion)status.units=status.units.filter(x=>x!==u);status.production.active_version_id='production-'+(++generation);}
      recalc();return reply({message:'所选操作已完成。'});
    }
    if(path==='/api/doctor')return reply({status,message:'检查完成。'});
    if(path==='/api/sync'){
      status.units.forEach(u=>{u.mounted_revision_id=u.mount_enabled===false ? null : u.production_revision_id;});
      status.units=status.units.filter(u=>!u.pending_deletion);status.mount.version_id='mount-'+(++generation);recalc();return reply({message:'同步完成。'});
    }
    return reply({error:'Unexpected API '+path},400);
  };
  await page.route('**/api/**',route);
  const row=id=>page.locator(`[data-skill="${id}"]`),button=name=>page.getByRole('button',{name,exact:true});
  const close=async()=>{if(await page.locator('dialog[open]').count())await button('完成').click();};
  const reload=async()=>{await page.reload();await page.locator('#skill-list').waitFor();};
  const execute=async(action,id='alpha')=>{
    if(['delete','discard'].includes(action))await row(id).locator('.skill-manage summary').click();
    await row(id).locator(`[data-action="skill-${action}"]`).click();
    await page.locator('dialog[data-state="confirm"]').waitFor();
    await page.locator('#dialog-actions button').last().click();
    await page.locator('dialog[data-state="success"]').waitFor();await close();
  };
  try{
    await page.setViewportSize({width:1440,height:1000});await reload();
    check((await row('alpha').innerText()).includes('开发有更新'),'Unpublished edits are visible');
    check((await row('gamma').innerText()).includes('三层内容一致'),'Equal content is visible');
    check(await page.locator('[data-action="remove"],[data-action="purge-skill"]').count()===0,'Ambiguous per-copy deletion removed');
    check(await page.locator('[data-action="add"],.review,#draft-count').count()===0,'Batch publication removed');
    check((await page.locator('[data-filter]').allTextContents()).join(',')==='全部,未发布,未同步','Only requested filters remain');
    await page.locator('[data-filter="unpublished"]').click();check(await page.locator('[data-skill]').count()===2,'New and modified Skill are unpublished');
    await page.locator('[data-filter="unsynced"]').click();check(await page.locator('[data-skill]').count()===0,'Unpublished-only changes are not pending sync');
    await page.locator('[data-filter="all"]').click();
    await row('alpha').locator('.detail-entry').click();await page.locator('#skill-source').waitFor();
    check(await page.locator('#skill-source').inputValue()==='development','Details defaults to unpublished content');
    await button('返回 Skill 集合').click();check(await row('alpha').locator('.detail-entry').evaluate(el=>el===document.activeElement),'Return focuses details entry');
    await row('alpha').locator('.skill-title-link').click();await page.locator('#skill-source').waitFor();
    await button('返回 Skill 集合').click();check(await row('alpha').locator('.skill-title-link').evaluate(el=>el===document.activeElement),'Name opens details and restores focus');
    await page.getByRole('searchbox').fill('项目报告');check(await page.locator('[data-skill]').count()===1,'Search by purpose');await page.getByRole('searchbox').fill('');
    await row('alpha').locator('[data-source="production"]').click();await page.locator('#skill-source').waitFor();
    check(await page.locator('#skill-source').inputValue()==='production','Source shortcut opens exact content');
    check(await page.locator('#content img').count()===0,'Description never executes markup');
    await button('返回 Skill 集合').click();
    check(await row('alpha').locator('[data-source="production"]').evaluate(el=>el===document.activeElement),'Return restores source button focus');
    passed.push('Skill-centered layers, source preview, focus and safe text');
    await row('alpha').locator('[data-action="skill-publish"]').click();await page.locator('dialog[data-state="confirm"]').waitFor();
    check(await button('取消').evaluate(el=>el===document.activeElement),'Cancel initially focused');
    await page.keyboard.press('Shift+Tab');check(await page.evaluate(()=>!!document.activeElement.closest('dialog')),'Dialog traps focus');
    await page.keyboard.press('Escape');check(status.units[0].production_revision_id==='alpha-old','Cancel does not publish');
    await execute('publish');
    check(status.units[0].mounted_revision_id==='alpha-old','Publish does not sync');
    check((await row('alpha').innerText()).includes('CLI 待同步'),'Pending sync stays visible');
    await execute('mount');check(status.units[0].mounted_revision_id==='alpha-new','Explicit selected sync');
    await execute('unmount');
    check(await row('alpha').count()===1 && status.units[0].production_revision_id==='alpha-new','Unmount preserves row and production');
    await execute('mount');passed.push('Confirmation, explicit publication/sync, unmount and remount');
    status=JSON.parse(JSON.stringify(base));recalc();await reload();
    await execute('discard');check(status.units[0].development_revision_id==='alpha-old','Discard restores production content');
    await execute('delete');check((await row('alpha').innerText()).includes('删除待完成'),'Deletion must keep pending row');
    await reload();check(await row('alpha').count()===1,'Pending deletion survives page reload');
    await execute('unmount');check(await row('alpha').count()===0,'Completed deletion exits list');
    await row('beta').locator('.skill-manage summary').click();check(await row('beta').locator('[data-action="skill-discard"]').count()===0,'No production means no discard');
    await row('beta').locator('.skill-manage summary').click();await execute('delete','beta');check(await row('beta').count()===0,'Unpublished unmounted deletion completes');
    passed.push('Discard and two-step deletion including reload and new Skill');
    status=JSON.parse(JSON.stringify(base));recalc();await reload();
    await button('库与维护').click();await button('检查状态').click();await page.locator('dialog[data-state="success"]').waitFor();await close();
    await page.locator('nav [data-view="collection"]').click();
    await row('alpha').locator('[data-action="skill-publish"]').click();await page.locator('dialog[data-state="confirm"]').waitFor();await button('确认发布').click();
    await page.locator('dialog[data-state="working"]').waitFor();await page.keyboard.press('Escape');check(await page.locator('dialog[open]').count()===1,'Working blocks Escape');await page.locator('dialog[data-state="success"]').waitFor();await close();
    await execute('publish','beta');
    check(status.units[2].production_revision_id==='gamma-old' && status.units[0].mounted_revision_id==='alpha-old','Individual publications preserve other Skill and mounted bytes');
    await page.locator('[data-filter="unsynced"]').click();check(await page.locator('[data-skill]').count()===2,'Published changes need sync');await page.locator('[data-filter="all"]').click();
    passed.push('Three filters, visible details, individual publication and working feedback');
    await button('CLI 与系统').click();await button('同步全部待处理项').click();
    check(await page.locator('dialog .change-list li').count()===2,'Full sync lists each pending Skill');
    await button('取消').click();check(status.units[0].mounted_revision_id==='alpha-old','Cancel full sync preserves installation');
    await page.locator('nav [data-view="collection"]').click();
    writeFailure=true;await row('alpha').locator('[data-action="skill-mount"]').click();await page.locator('dialog[data-state="failure"]').waitFor();await button('关闭').click();writeFailure=false;
    readFailure=true;await button('刷新').click();await page.locator('dialog[data-state="failure"]').waitFor();await button('关闭').click();check(await page.locator('[data-action="skill-delete"]').count()===0,'Unreadable state pauses writes');readFailure=false;await button('刷新').click();await page.locator('dialog[data-state="success"]').waitFor();await close();
    passed.push('Write and read errors do not claim success');
    status.units.push(
      {unit_id:'stopped',development_revision_id:'s',production_revision_id:'s',mounted_revision_id:null,mount_enabled:false},
      {unit_id:'stopping',development_revision_id:'t',production_revision_id:'t',mounted_revision_id:'t',mount_enabled:false},
      {unit_id:'deleting',development_revision_id:null,production_revision_id:null,mounted_revision_id:'d',mount_enabled:false,pending_deletion:true});
    recalc();await reload();await page.locator('[data-filter="unsynced"]').click();
    check(await row('stopped').count()===0 && await row('stopping').count()===1 && await row('deleting').count()===1,'Intentional unmount is settled; pending cleanup still needs sync');
    await page.locator('[data-filter="all"]').click();
    const theme=await page.locator('html').getAttribute('data-theme');await page.locator('#theme-toggle').click();await reload();check(await page.locator('html').getAttribute('data-theme')!==theme,'Theme persistence');
    await page.setViewportSize({width:390,height:844});check(await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth),'Narrow layout fits');
    await row('alpha').locator('[data-action="skill-mount"]').click();await page.locator('dialog[data-state="confirm"]').waitFor();
    check(await page.locator('dialog').evaluate(node=>{const r=node.getBoundingClientRect();return r.left>=0 && r.right<=innerWidth;}),'Narrow dialog fits');await page.keyboard.press('Escape');
    await page.emulateMedia({reducedMotion:'reduce'});await row('alpha').locator('[data-action="skill-mount"]').click();await page.locator('dialog[data-state="confirm"]').waitFor();check(await page.locator('dialog').evaluate(node=>parseFloat(getComputedStyle(node).animationDuration)<.01),'Reduced motion honored');await page.keyboard.press('Escape');
    passed.push('Theme persistence, mobile layout, reduced motion');
    check(errors.length===0,errors.join('\n'));check(nativeDialogs.length===0,'Only shared application dialogs');
    return {passed,postCount:posts.length,pageErrors:errors};
  }finally{await page.unroute('**/api/**',route);page.off('pageerror',onError);page.off('dialog',onDialog);await page.setViewportSize({width:1440,height:1000});await page.emulateMedia({reducedMotion:'no-preference'});await page.reload();}
}
