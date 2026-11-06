package main

import (
 "encoding/json"
 "fmt"
 "os"
 "github.com/ollama/ollama/api"
 "github.com/ollama/ollama/model/renderers"
)

func main() {
 // The renderer is required: a default would silently format another family's prompt.
 if len(os.Args) != 2 { fmt.Fprintln(os.Stderr, "usage: renderer-by-name RENDERER"); os.Exit(2) }
 var request api.ChatRequest
 if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil { panic(err) }
 prompt, err := renderers.RenderWithRenderer(os.Args[1], request.Messages, request.Tools, request.Think)
 if err != nil { panic(err) }
 if err := json.NewEncoder(os.Stdout).Encode(map[string]string{"prompt":prompt}); err != nil { fmt.Fprintln(os.Stderr,err); os.Exit(1) }
}
