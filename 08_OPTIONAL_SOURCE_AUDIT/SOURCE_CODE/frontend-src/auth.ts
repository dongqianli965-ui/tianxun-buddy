import {reactive} from 'vue';
const TOKEN='txbuddy.accessToken';const USER='txbuddy.authUser';
export const auth=reactive({token:sessionStorage.getItem(TOKEN)||'',username:sessionStorage.getItem(USER)||'',message:''});
export function setAuth(token:string,username:string){auth.token=token;auth.username=username;auth.message='';sessionStorage.setItem(TOKEN,token);sessionStorage.setItem(USER,username);}
export function clearAuth(message=''){auth.token='';auth.username='';auth.message=message;sessionStorage.removeItem(TOKEN);sessionStorage.removeItem(USER);}
