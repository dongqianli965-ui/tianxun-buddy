<script setup lang="ts">
import {onMounted, ref} from 'vue';
import {api} from '../api';
import {auth,setAuth} from '../auth';
const username=ref('');const password=ref('');const busy=ref(false);const error=ref('');
async function login(){busy.value=true;error.value='';try {
  const result=await api.login(username.value,password.value);setAuth(result.access_token,username.value);password.value='';
}catch(e){error.value=e instanceof Error?e.message:String(e);}finally{busy.value=false;}}
onMounted(async()=>{
  const params=new URLSearchParams(window.location.search);
  const bootstrap=params.get('bootstrap');
  const requested=params.get('test_only_autologin') || 'owner';
  if(!bootstrap)return;
  busy.value=true;
  try{
    const result=await api.testAutoLogin(requested,bootstrap);
    setAuth(result.access_token,requested);
    window.history.replaceState({},'',window.location.pathname+window.location.hash);
  }catch(e){error.value=e instanceof Error?e.message:String(e);}
  finally{busy.value=false;}
});
</script>
<template>
  <main class="login-shell"><form class="card login-card" @submit.prevent="login">
    <div class="logo">TX</div><h1>天寻 Buddy</h1><p class="hint" data-testid="login-version">真实资料样本工作台 · V4.2.6</p>
    <p class="notice">TEST_ONLY 人工测试入口。登录后核对后端环境状态；正式业务验收未通过，精度 E：BLOCKED。</p>
    <p v-if="auth.message" class="err" role="alert">{{auth.message}}</p>
    <label>用户名<input data-testid="login-user" v-model="username" autocomplete="username" required /></label>
    <label>密码<input data-testid="login-password" v-model="password" type="password" autocomplete="current-password" required /></label>
    <p v-if="error" class="err" role="alert">{{error}}</p>
    <button class="btn primary" :disabled="busy" data-testid="login-submit">{{busy?'登录中…':'登录'}}</button>
    <p class="hint">临时账号见本机私有测试说明文件；测试操作不计入真实人工专业复核。</p>
  </form></main>
</template>
<style scoped>.login-shell{min-height:100vh;display:grid;place-items:center;background:#eef2f7}.login-card{width:min(400px,90vw);padding:32px}.login-card h1{margin:20px 0 8px}.login-card label{display:block;margin:18px 0}.login-card input{width:100%;margin-top:8px}</style>
