package main

import (
 "encoding/json"
 "fmt"
 "os"
 "github.com/ollama/ollama/api"
 "github.com/ollama/ollama/model/renderers"
)

func main() {
 var request api.ChatRequest
 if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil { panic(err) }
 prompt, err := renderers.RenderWithRenderer("qwen3.5", request.Messages, request.Tools, request.Think)
 if err != nil { panic(err) }
 if err := json.NewEncoder(os.Stdout).Encode(map[string]string{"prompt":prompt}); err != nil { fmt.Fprintln(os.Stderr,err); os.Exit(1) }
}
