package main

import (
  "bytes"
  "context"
  "encoding/json"
  "errors"
  "fmt"
  "io"
  "net/http"
  "net/url"
  "os"
  "strings"
  "sync"
  "time"
)

type keyState struct{ Value string; Cooldown time.Time; Disabled bool }
type keyPool struct{ mu sync.Mutex; keys []keyState; next int }

func newPool(raw string) *keyPool {
  raw = strings.NewReplacer("\r", "\n", ";", ",", "\n", ",").Replace(raw)
  seen := map[string]bool{}
  p := &keyPool{}
  for _, s := range strings.Split(raw, ",") {
    s = strings.TrimSpace(s)
    if s != "" && !seen[s] { seen[s] = true; p.keys = append(p.keys, keyState{Value:s}) }
  }
  return p
}
func (p *keyPool) candidates() []int {
  p.mu.Lock(); defer p.mu.Unlock()
  n:=len(p.keys); if n==0{return nil}; now:=time.Now(); out:=[]int{}
  for step:=0; step<n; step++ { i:=(p.next+step)%n; k:=p.keys[i]; if !k.Disabled && !now.Before(k.Cooldown){out=append(out,i)} }
  if len(out)>0 { p.next=(out[0]+1)%n }
  return out
}
func (p *keyPool) value(i int) string { p.mu.Lock(); defer p.mu.Unlock(); return p.keys[i].Value }
func (p *keyPool) cooldown(i int,d time.Duration){p.mu.Lock();defer p.mu.Unlock();p.keys[i].Cooldown=time.Now().Add(d)}
func (p *keyPool) disable(i int){p.mu.Lock();defer p.mu.Unlock();p.keys[i].Disabled=true}

type app struct{ pool *keyPool; model, secret string; client *http.Client }
type rpcReq struct{ JSONRPC string `json:"jsonrpc"`; ID json.RawMessage `json:"id,omitempty"`; Method string `json:"method"`; Params json.RawMessage `json:"params,omitempty"` }
type chatArgs struct{ Prompt string `json:"prompt"`; System string `json:"system_instruction,omitempty"`; Temperature *float64 `json:"temperature,omitempty"`; MaxOutputTokens *int `json:"max_output_tokens,omitempty"` }

func main(){
  a:=&app{pool:newPool(os.Getenv("GEMINI_KEYS")),model:env("DEFAULT_MODEL","gemini-3.8-flash"),secret:strings.TrimSpace(os.Getenv("CHATGPT_PATH_SECRET")),client:&http.Client{Timeout:90*time.Second}}
  port:=env("PORT","10000")
  http.HandleFunc("/health",func(w http.ResponseWriter,r *http.Request){json.NewEncoder(w).Encode(map[string]any{"ok":true,"model":a.model,"keys":len(a.pool.keys)})})
  http.HandleFunc("/",a.mcp)
  _=http.ListenAndServe(":"+port,nil)
}
func env(k,f string)string{if v:=strings.TrimSpace(os.Getenv(k));v!=""{return v};return f}
func (a *app) mcp(w http.ResponseWriter,r *http.Request){
  w.Header().Set("Cache-Control","no-store")
  if a.secret=="" || r.URL.Path!="/mcp/"+a.secret { http.NotFound(w,r); return }
  if r.Method!="POST" { http.Error(w,"method not allowed",405); return }
  var q rpcReq; if err:=json.NewDecoder(io.LimitReader(r.Body,1<<20)).Decode(&q);err!=nil{rpcErr(w,nil,-32700,"parse error");return}
  switch q.Method {
  case "initialize": rpcResult(w,q.ID,map[string]any{"protocolVersion":"2025-06-18","capabilities":map[string]any{"tools":map[string]any{"listChanged":false}},"serverInfo":map[string]any{"name":"gemini-3.8-flash-gateway","version":"1.0.0"}})
  case "notifications/initialized": w.WriteHeader(202)
  case "ping": rpcResult(w,q.ID,map[string]any{})
  case "tools/list": rpcResult(w,q.ID,map[string]any{"tools":[]any{map[string]any{"name":"gemini_chat","description":"Send a prompt to Gemini 3.8 Flash using the configured Gemini API keys.","inputSchema":map[string]any{"type":"object","properties":map[string]any{"prompt":map[string]any{"type":"string"},"system_instruction":map[string]any{"type":"string"},"temperature":map[string]any{"type":"number"},"max_output_tokens":map[string]any{"type":"integer"}},"required":[]string{"prompt"},"additionalProperties":false}}}})
  case "tools/call":
    var p struct{Name string `json:"name"`; Arguments json.RawMessage `json:"arguments"`}; if json.Unmarshal(q.Params,&p)!=nil || p.Name!="gemini_chat"{rpcErr(w,q.ID,-32602,"invalid tool call");return}
    var in chatArgs; if json.Unmarshal(p.Arguments,&in)!=nil{rpcErr(w,q.ID,-32602,"invalid arguments");return}
    text,err:=a.generate(r.Context(),in); if err!=nil{rpcResult(w,q.ID,map[string]any{"content":[]any{map[string]any{"type":"text","text":"Gemini request failed: "+err.Error()}},"isError":true});return}
    rpcResult(w,q.ID,map[string]any{"content":[]any{map[string]any{"type":"text","text":text}},"isError":false})
  default: rpcErr(w,q.ID,-32601,"method not found")
  }
}
func (a *app) generate(ctx context.Context,in chatArgs)(string,error){
  in.Prompt=strings.TrimSpace(in.Prompt); if in.Prompt==""{return "",errors.New("prompt is required")}
  payload:=map[string]any{"contents":[]any{map[string]any{"role":"user","parts":[]any{map[string]any{"text":in.Prompt}}}}}
  if strings.TrimSpace(in.System)!=""{payload["systemInstruction"]=map[string]any{"parts":[]any{map[string]any{"text":strings.TrimSpace(in.System)}}}}
  g:=map[string]any{}; if in.Temperature!=nil{g["temperature"]=*in.Temperature}; if in.MaxOutputTokens!=nil{g["maxOutputTokens"]=*in.MaxOutputTokens}; if len(g)>0{payload["generationConfig"]=g}
  body,_:=json.Marshal(payload); ids:=a.pool.candidates(); if len(ids)==0{return "",errors.New("no Gemini key available")}; var last error
  for _,i:=range ids{
    endpoint:="https://generativelanguage.googleapis.com/v1beta/models/"+url.PathEscape(a.model)+":generateContent"
    req,_:=http.NewRequestWithContext(ctx,"POST",endpoint,bytes.NewReader(body)); req.Header.Set("Content-Type","application/json"); req.Header.Set("x-goog-api-key",a.pool.value(i))
    resp,err:=a.client.Do(req); if err!=nil{last=err;a.pool.cooldown(i,10*time.Second);continue}
    raw,_:=io.ReadAll(io.LimitReader(resp.Body,4<<20)); resp.Body.Close()
    if resp.StatusCode>=200&&resp.StatusCode<300{return extract(raw)}
    last=fmt.Errorf("Gemini HTTP %d",resp.StatusCode)
    if resp.StatusCode==429{a.pool.cooldown(i,60*time.Second);continue}; if resp.StatusCode==401||resp.StatusCode==403{a.pool.disable(i);continue}; if resp.StatusCode>=500{a.pool.cooldown(i,15*time.Second);continue}; return "",last
  }
  if last==nil{last=errors.New("all Gemini keys failed")}; return "",last
}
func extract(raw []byte)(string,error){var x struct{Candidates []struct{Content struct{Parts []struct{Text string `json:"text"`} `json:"parts"`} `json:"content"`} `json:"candidates"`};if json.Unmarshal(raw,&x)!=nil||len(x.Candidates)==0{return "",errors.New("invalid Gemini response")};var b strings.Builder;for _,p:=range x.Candidates[0].Content.Parts{b.WriteString(p.Text)};if b.Len()==0{return "",errors.New("empty Gemini response")};return b.String(),nil}
func rpcResult(w http.ResponseWriter,id json.RawMessage,result any){w.Header().Set("Content-Type","application/json");json.NewEncoder(w).Encode(map[string]any{"jsonrpc":"2.0","id":json.RawMessage(id),"result":result})}
func rpcErr(w http.ResponseWriter,id json.RawMessage,code int,msg string){w.Header().Set("Content-Type","application/json");json.NewEncoder(w).Encode(map[string]any{"jsonrpc":"2.0","id":json.RawMessage(id),"error":map[string]any{"code":code,"message":msg}})}
