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

type keyState struct {
	Value    string
	Cooldown time.Time
	Disabled bool
}

type keyPool struct {
	mu   sync.Mutex
	keys []keyState
	next int
}

func newPool(raw string) *keyPool {
	raw = strings.NewReplacer("\r", "\n", ";", ",", "\n", ",").Replace(raw)
	seen := map[string]bool{}
	p := &keyPool{}
	for _, s := range strings.Split(raw, ",") {
		s = strings.TrimSpace(s)
		if s != "" && !seen[s] {
			seen[s] = true
			p.keys = append(p.keys, keyState{Value: s})
		}
	}
	return p
}

func (p *keyPool) candidates() []int {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := len(p.keys)
	if n == 0 {
		return nil
	}
	now := time.Now()
	out := make([]int, 0, n)
	for step := 0; step < n; step++ {
		i := (p.next + step) % n
		k := p.keys[i]
		if !k.Disabled && !now.Before(k.Cooldown) {
			out = append(out, i)
		}
	}
	if len(out) > 0 {
		p.next = (out[0] + 1) % n
	}
	return out
}

func (p *keyPool) value(i int) string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.keys[i].Value
}

func (p *keyPool) cooldown(i int, d time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keys[i].Cooldown = time.Now().Add(d)
}

func (p *keyPool) disable(i int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keys[i].Disabled = true
}

type app struct {
	pool   *keyPool
	model  string
	secret string
	client *http.Client
}

type rpcReq struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type chatArgs struct {
	Prompt string `json:"prompt"`
	System string `json:"system_instruction,omitempty"`
}

type geminiResponse struct {
	Candidates []struct {
		Content struct {
			Parts []struct {
				Text    string `json:"text"`
				Thought bool   `json:"thought,omitempty"`
			} `json:"parts"`
		} `json:"content"`
		FinishReason string `json:"finishReason"`
	} `json:"candidates"`
	UsageMetadata struct {
		PromptTokenCount     int `json:"promptTokenCount"`
		CandidatesTokenCount int `json:"candidatesTokenCount"`
		ThoughtsTokenCount   int `json:"thoughtsTokenCount"`
		TotalTokenCount      int `json:"totalTokenCount"`
	} `json:"usageMetadata"`
}

func main() {
	a := &app{
		pool:   newPool(os.Getenv("GEMINI_KEYS")),
		model:  env("DEFAULT_MODEL", "gemini-3.8-flash"),
		secret: strings.TrimSpace(os.Getenv("CHATGPT_PATH_SECRET")),
		client: &http.Client{Timeout: 240 * time.Second},
	}
	port := env("PORT", "10000")
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "model": a.model, "keys": len(a.pool.keys)})
	})
	http.HandleFunc("/", a.mcp)
	_ = http.ListenAndServe(":"+port, nil)
}

func env(k, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return fallback
}

func (a *app) mcp(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if a.secret == "" || r.URL.Path != "/mcp/"+a.secret {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var q rpcReq
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&q); err != nil {
		rpcErr(w, nil, -32700, "parse error")
		return
	}
	switch q.Method {
	case "initialize":
		rpcResult(w, q.ID, map[string]any{
			"protocolVersion": "2025-06-18",
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo":      map[string]any{"name": "gemini-3.8-flash-gateway", "version": "1.1.0"},
			"instructions":    "Use gemini_chat to obtain a complete answer from Gemini 3.8 Flash. Pass the full user request in prompt. Do not request partial output.",
		})
	case "notifications/initialized":
		w.WriteHeader(http.StatusAccepted)
	case "ping":
		rpcResult(w, q.ID, map[string]any{})
	case "tools/list":
		rpcResult(w, q.ID, map[string]any{"tools": []any{
			map[string]any{
				"name":        "gemini_chat",
				"description": "Send the complete user request to Gemini 3.8 Flash and return its complete final answer.",
				"inputSchema": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"prompt":             map[string]any{"type": "string", "description": "The full request to answer."},
						"system_instruction": map[string]any{"type": "string", "description": "Optional instruction for response style or constraints."},
					},
					"required":             []string{"prompt"},
					"additionalProperties": false,
				},
			},
		}})
	case "tools/call":
		var p struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if json.Unmarshal(q.Params, &p) != nil || p.Name != "gemini_chat" {
			rpcErr(w, q.ID, -32602, "invalid tool call")
			return
		}
		var in chatArgs
		if json.Unmarshal(p.Arguments, &in) != nil {
			rpcErr(w, q.ID, -32602, "invalid arguments")
			return
		}
		text, meta, err := a.generate(r.Context(), in)
		if err != nil {
			rpcResult(w, q.ID, map[string]any{
				"content": []any{map[string]any{"type": "text", "text": "Gemini request failed: " + err.Error()}},
				"isError": true,
			})
			return
		}
		rpcResult(w, q.ID, map[string]any{
			"content":           []any{map[string]any{"type": "text", "text": text}},
			"structuredContent": map[string]any{"answer": text, "meta": meta},
			"isError":           false,
		})
	default:
		rpcErr(w, q.ID, -32601, "method not found")
	}
}

func (a *app) generate(ctx context.Context, in chatArgs) (string, map[string]any, error) {
	in.Prompt = strings.TrimSpace(in.Prompt)
	if in.Prompt == "" {
		return "", nil, errors.New("prompt is required")
	}

	// Gemini 3.x works best with default sampling parameters. Keep a generous
	// output ceiling because internal thinking can consume output-token budget.
	payload := map[string]any{
		"contents": []any{map[string]any{
			"role":  "user",
			"parts": []any{map[string]any{"text": in.Prompt}},
		}},
		"generationConfig": map[string]any{
			"maxOutputTokens": 65536,
			"thinkingConfig": map[string]any{
				"thinkingLevel": "medium",
			},
		},
	}
	if strings.TrimSpace(in.System) != "" {
		payload["systemInstruction"] = map[string]any{"parts": []any{map[string]any{"text": strings.TrimSpace(in.System)}}}
	}
	body, _ := json.Marshal(payload)

	ids := a.pool.candidates()
	if len(ids) == 0 {
		return "", nil, errors.New("no Gemini key available")
	}
	var last error
	for _, i := range ids {
		endpoint := "https://generativelanguage.googleapis.com/v1beta/models/" + url.PathEscape(a.model) + ":generateContent"
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return "", nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("x-goog-api-key", a.pool.value(i))
		resp, err := a.client.Do(req)
		if err != nil {
			last = err
			a.pool.cooldown(i, 10*time.Second)
			continue
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			text, meta, err := extract(raw)
			if err == nil {
				return text, meta, nil
			}
			last = err
			continue
		}
		last = fmt.Errorf("Gemini HTTP %d: %s", resp.StatusCode, compactError(raw))
		if resp.StatusCode == http.StatusTooManyRequests {
			a.pool.cooldown(i, 60*time.Second)
			continue
		}
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			a.pool.disable(i)
			continue
		}
		if resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode >= 500 {
			a.pool.cooldown(i, 15*time.Second)
			continue
		}
		return "", nil, last
	}
	if last == nil {
		last = errors.New("all Gemini keys failed")
	}
	return "", nil, last
}

func extract(raw []byte) (string, map[string]any, error) {
	var x geminiResponse
	if err := json.Unmarshal(raw, &x); err != nil || len(x.Candidates) == 0 {
		return "", nil, errors.New("invalid Gemini response")
	}
	var b strings.Builder
	for _, p := range x.Candidates[0].Content.Parts {
		if !p.Thought && p.Text != "" {
			b.WriteString(p.Text)
		}
	}
	text := strings.TrimSpace(b.String())
	finish := x.Candidates[0].FinishReason
	meta := map[string]any{
		"model":                  "gemini-3.8-flash",
		"finish_reason":          finish,
		"prompt_tokens":          x.UsageMetadata.PromptTokenCount,
		"answer_tokens":          x.UsageMetadata.CandidatesTokenCount,
		"thinking_tokens":        x.UsageMetadata.ThoughtsTokenCount,
		"total_tokens":           x.UsageMetadata.TotalTokenCount,
		"max_output_tokens":      65536,
		"thinking_level":         "medium",
	}
	if text == "" {
		return "", meta, fmt.Errorf("empty Gemini response (finishReason=%s)", finish)
	}
	if finish == "MAX_TOKENS" {
		return "", meta, errors.New("Gemini hit MAX_TOKENS before completing the answer")
	}
	return text, meta, nil
}

func compactError(raw []byte) string {
	var x struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &x) == nil && strings.TrimSpace(x.Error.Message) != "" {
		return strings.TrimSpace(x.Error.Message)
	}
	s := strings.TrimSpace(string(raw))
	if len(s) > 300 {
		s = s[:300]
	}
	return s
}

func rpcResult(w http.ResponseWriter, id json.RawMessage, result any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": json.RawMessage(id), "result": result})
}

func rpcErr(w http.ResponseWriter, id json.RawMessage, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"jsonrpc": "2.0",
		"id":      json.RawMessage(id),
		"error":   map[string]any{"code": code, "message": msg},
	})
}
