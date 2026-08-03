# Extensão do Lifelog — detector de reunião

Avisa o Lifelog quando você entra numa reunião no navegador, para que ele
grave só nesse período.

## Por que ela existe

O Lifelog detecta reuniões sozinho quando o Zoom ou o Teams estão instalados
como aplicativo — o Windows registra qual processo abriu o microfone.

No navegador isso não basta. Um Meet e um vídeo do YouTube são o mesmo
`chrome.exe`, e o Lifelog só consegue ler o título da **maior** janela do
processo, não da aba que está em chamada. Com várias janelas abertas, um Meet
numa janela menor fica invisível — e perder a reunião inteira é o pior erro
possível.

A extensão sabe exatamente qual aba está em chamada.

## Instalar

1. Abra `chrome://extensions` (ou `edge://extensions`).
2. Ligue o **Modo do desenvolvedor**, no canto superior direito.
3. Clique em **Carregar sem compactação** e escolha esta pasta.

Pronto. Ela funciona em segundo plano, sem ícone nem janela.

## O que ela vê e o que reporta

Reporta ao Lifelog, rodando em `127.0.0.1:8000`, apenas três coisas:

- se há uma reunião em curso;
- em qual serviço (`meet`, `teams`, `zoom`…);
- o título da aba.

**Nunca o conteúdo da página.** A extensão não pede permissão para lê-lo — só
`tabs`, que dá acesso a título e URL, e `alarms`, para acordar periodicamente.
O único endereço que ela pode contatar é o seu Lifelog local.

## Serviços reconhecidos

Meet, Teams, Zoom, Webex, Whereby, Jitsi, Gather e Discord.

A URL precisa ser de uma sala, não da página inicial: `meet.google.com`
aberto sem código de reunião não conta, porque muita gente deixa essa aba
aberta o dia todo.

## Se o Lifelog estiver fechado

A extensão tenta reportar e falha em silêncio. Nada quebra, e o relato antigo
expira sozinho no servidor depois de 45 segundos — sem isso o Lifelog ficaria
gravando para sempre por causa de uma reunião que já acabou.
