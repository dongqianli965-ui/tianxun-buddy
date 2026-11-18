import {authorizedFetch} from './api';
export async function governance<T=any>(path:string,body?:unknown):Promise<T>{
  const response=await authorizedFetch('/v1/governance'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return response.json();
}
export const reviewLabels:Record<string,string>={NEEDS_EXPERT_REVIEW:'待复核',IN_REVIEW:'复核中',REVIEWED:'已复核',RETURNED:'复核退回',PROFESSIONALLY_SIGNED:'正式专业签认'};
