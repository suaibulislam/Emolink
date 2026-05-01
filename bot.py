import discord
from discord.ext import commands, tasks
from discord import app_commands
from typing import Optional, Dict, Tuple, List, Union
import time
import re
import os
import threading
import signal
import atexit
import json
import copy
import io
import asyncio
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
import aiohttp
import aiosqlite
from PIL import Image
from dotenv import load_dotenv
load_dotenv()

# Firebase
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    print("Firebase Admin SDK not installed. Install with: pip install firebase-admin")

BOT_TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID_ENV = os.getenv('GUILD_ID')
GUILD_ID = int(GUILD_ID_ENV) if GUILD_ID_ENV and GUILD_ID_ENV.isdigit() else None
HEALTH_CHECK_PORT = int(os.getenv('HEALTH_CHECK_PORT', '0'))  # 0 = disabled

# Backend server configuration (Discord guild/channel for storing processed stickers)
BACKEND_GUILD_ID_ENV = os.getenv('BACKEND_GUILD_ID')
BACKEND_GUILD_ID = int(BACKEND_GUILD_ID_ENV) if BACKEND_GUILD_ID_ENV and BACKEND_GUILD_ID_ENV.isdigit() else None
BACKEND_CHANNEL_ID_ENV = os.getenv('BACKEND_CHANNEL_ID')
BACKEND_CHANNEL_ID = int(BACKEND_CHANNEL_ID_ENV) if BACKEND_CHANNEL_ID_ENV and BACKEND_CHANNEL_ID_ENV.isdigit() else None

# Firebase configuration
FIREBASE_CREDENTIALS_PATH = os.getenv('FIREBASE_CREDENTIALS_PATH', 'firebase-credentials.json')
FIREBASE_PROJECT_ID = os.getenv('FIREBASE_PROJECT_ID')

# Sticker configuration
STICKER_DEFAULT_SIZE = 160
STICKER_SIZES = [48, 128, 160, 240, 320]  # Valid sticker sizes (removed 24, 96; added 128)
STICKER_MIN_SIZE = 48
STICKER_MAX_SIZE = 320

# Uptime tracking
start_time = None

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix='!emoji ', intents=intents, help_command=None)
bot_instance = bot  # Set for health check

class SizeSelectionView(discord.ui.View):
    def __init__(self, emoji_data, original_size=128, expires_at: Optional[int] = None, persistent: bool = False, is_ephemeral: bool = False):
        # Ephemeral messages expire after 5 minutes, persistent ones never expire (timeout=None)
        if is_ephemeral:
            super().__init__(timeout=300)
            self.expires_at = expires_at if expires_at else (int(time.time()) + 300)
            self.persistent = False  # Ephemeral messages are never persistent
        else:
            super().__init__(timeout=None if persistent else 300)  # None = never expires
            self.expires_at = None if persistent else expires_at
            self.persistent = persistent
        
        self.emoji_data = emoji_data
        self.original_size = original_size
        self.is_ephemeral = is_ephemeral
        self.message: Optional[discord.Message] = None
        self.message_id: Optional[int] = None  # Set when message is sent (needed for custom_id)
        self.base_url = remove_size_param(emoji_data['url'])
        if 'gif_url' in emoji_data:
            self.base_gif_url = remove_size_param(emoji_data['gif_url'])
        else:
            self.base_gif_url = None
        
        # Generate unique custom_id for persistent views (based on emoji ID or URL hash)
        emoji_id = emoji_data.get('id') or hash(emoji_data.get('url', '')) % 1000000
        self.view_id = f"size_sel_{emoji_id}"
    
    async def on_message_set(self, message: discord.Message, is_ephemeral: bool = False) -> None:
        """Called after message is sent - save to database for persistence if not ephemeral."""
        self.message = message
        self.message_id = message.id
        self.is_ephemeral = is_ephemeral
        
        # Configure custom_id for persistent views to enable button functionality after bot restarts
        if self.persistent and not is_ephemeral:
            # Set custom_id on all buttons using message_id
            self._setup_button_ids()
            # Update message with buttons that now have custom_id
            try:
                await message.edit(view=self)
                # Register view with bot to connect custom_id buttons to interaction handlers
                # This enables button functionality to persist across bot restarts
                bot.add_view(self, message_id=self.message_id)
            except Exception as e:
                print(f"Error setting up persistent view buttons for message {self.message_id}: {e}")
                import traceback
                traceback.print_exc()
        
        # Only save to database if not ephemeral and persistent
        if self.persistent and not is_ephemeral:
            view_data = {
                'emoji_data': self.emoji_data,
                'original_size': self.original_size
            }
            await save_persistent_view(
                message.id,
                message.channel.id,
                'size_selection',
                view_data,
                None  # Never expires for persistent views
            )
        
    def _generate_view_id(self) -> str:
        """Generate a unique view ID based on emoji data (used for custom_id)."""
        emoji_id = self.emoji_data.get('id') or str(hash(self.emoji_data.get('url', ''))) % 1000000
        return f"emoji_{emoji_id}"
    
    def _setup_button_ids(self):
        """Set custom_id for all buttons based on view ID and message_id."""
        if not self.persistent or not self.message_id:
            return
        
        view_id = self._generate_view_id()
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label:
                # Generate custom_id: view_id_button_label_message_id
                label_part = item.label.lower().replace('px', '').replace(' ', '_')
                item.custom_id = f"{view_id}_{label_part}_{self.message_id}"
    
    @discord.ui.button(label='24px', style=discord.ButtonStyle.secondary, custom_id=None)
    async def size_24(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 24)
        
    @discord.ui.button(label='48px', style=discord.ButtonStyle.secondary, custom_id=None)
    async def size_48(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 48)
        
    @discord.ui.button(label='56px', style=discord.ButtonStyle.secondary, custom_id=None)
    async def size_56(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 56)
        
    @discord.ui.button(label='128px', style=discord.ButtonStyle.secondary, custom_id=None)
    async def size_128(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 128)
        
    @discord.ui.button(label='Original', style=discord.ButtonStyle.secondary, custom_id=None)
    async def size_original(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_original(interaction)
    
    async def update_size(self, interaction: discord.Interaction, new_size: int):
        # Check if view is already stopped or buttons are disabled
        if self.is_finished() or any(item.disabled for item in self.children if isinstance(item, discord.ui.Button)):
            return
            
        # Check expiration for ephemeral messages only
        if self.is_ephemeral and self.expires_at and int(time.time()) > self.expires_at:
            await notify_view_expired(interaction, self)
            return
            
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
            
        # Always use base URL and apply the new size
        new_url = get_emoji_size(self.base_url, new_size)
        updated_data = self.emoji_data.copy()
        updated_data['url'] = new_url
        
        embed = build_single_embed(updated_data, new_size, self.expires_at)
        # Ensure thumbnail is set with the correctly sized URL
        embed.set_thumbnail(url=new_url)
        if self.base_gif_url:
            gif_url_sized = get_emoji_size(self.base_gif_url, new_size)
            embed.add_field(name="GIF URL", value=f"[Open link]({gif_url_sized})", inline=True)

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                # Update button styles
                if item.label == 'Original':
                    item.style = discord.ButtonStyle.secondary
                else:
                    item.style = discord.ButtonStyle.primary if f"{new_size}px" in item.label else discord.ButtonStyle.secondary

        try:
            await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)
        except:
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except:
                pass

    async def on_timeout(self) -> None:
        """Handle view timeout - remove from database if persistent."""
        if self.persistent and self.message:
            # Remove from database
            await delete_persistent_view(self.message.id)
        
        try:
            disable_view_components(self)
            # Update the message to show disabled buttons
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.errors.NotFound:
                    # Message was deleted, ignore
                    pass
                except Exception as e:
                    print(f"Error editing message on timeout: {e}")
        except Exception as e:
            print(f"Error in on_timeout: {e}")

    async def update_original(self, interaction: discord.Interaction):
        # Check if view is already stopped or buttons are disabled
        if self.is_finished() or any(item.disabled for item in self.children if isinstance(item, discord.ui.Button)):
            return
        
        # Defer to show loading state
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass  # Already responded
        
        # Check expiration for ephemeral messages only
        if self.is_ephemeral and self.expires_at and int(time.time()) > self.expires_at:
            await notify_view_expired(interaction, self)
            return
            
        # Use base URL (already has size removed) for the URL field
        # But use a sized version for thumbnail (Discord needs size for thumbnails)
        updated_data = self.emoji_data.copy()
        updated_data['url'] = self.base_url  # URL without size for copying
        # For thumbnail, use 128px so it displays properly
        thumbnail_url = get_emoji_size(self.base_url, 128)

        embed = build_single_embed(updated_data, 'original', self.expires_at)
        # Override thumbnail to use sized version for display
        embed.set_thumbnail(url=thumbnail_url)
        
        if self.base_gif_url:
            embed.add_field(name="GIF URL", value=f"[Open link]({self.base_gif_url})", inline=True)

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                # Update button styles
                item.style = discord.ButtonStyle.primary if item.label == 'Original' else discord.ButtonStyle.secondary
        
        try:
            await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)
        except:
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except:
                pass

class StickerSizeSelectionView(discord.ui.View):
    """Separate view for stickers with their own size buttons (48, 128, 160, 240, 320)"""
    def __init__(self, sticker_data, original_size=160, expires_at: Optional[int] = None, persistent: bool = False, is_ephemeral: bool = False):
        # Ephemeral messages expire after 5 minutes, persistent ones never expire
        if is_ephemeral:
            super().__init__(timeout=300)
            self.expires_at = expires_at if expires_at else (int(time.time()) + 300)
            self.persistent = False
        else:
            super().__init__(timeout=None if persistent else 300)  # None = never expires
            self.expires_at = None if persistent else expires_at
            self.persistent = persistent
        
        self.sticker_data = sticker_data
        self.original_size = original_size
        self.is_ephemeral = is_ephemeral
        self.message: Optional[discord.Message] = None
        self.message_id: Optional[int] = None  # Set when message is sent (needed for custom_id)
        # Check if using backend URLs
        self.backend_urls = sticker_data.get('backend_urls', {})
        self.uses_backend = sticker_data.get('uses_backend', False)
        # Store uploaded message IDs (stored permanently, never deleted)
        self.uploaded_message_ids = sticker_data.get('uploaded_message_ids', [])
        
        if self.uses_backend and self.backend_urls:
            # Using backend URLs - no need for base_url manipulation
            self.base_url = None
        else:
            # Using Discord CDN - need base URL for size manipulation
            self.base_url = remove_size_param(sticker_data['url'])
        
        # Check if sticker is animated (for reference, but don't disable buttons yet)
        self.is_animated = sticker_data.get('animated', False) or '.png' in sticker_data.get('url', '')
    
    def _generate_view_id(self) -> str:
        """Generate a unique view ID based on sticker data (used for custom_id)."""
        sticker_id = self.sticker_data.get('id') or str(abs(hash(self.sticker_data.get('url', ''))))[:10]
        return f"sticker_{sticker_id}"
    
    def _setup_button_ids(self):
        """Set custom_id for all buttons based on view ID and message_id."""
        if not self.persistent or not self.message_id:
            return
        
        view_id = self._generate_view_id()
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label:
                label_clean = item.label.lower().replace('px', '').replace(' ', '_')
                item.custom_id = f"{view_id}_{label_clean}_{self.message_id}"
    
    async def on_message_set(self, message: discord.Message, is_ephemeral: bool = False) -> None:
        """Called after message is sent - save to database for persistence if not ephemeral."""
        self.message = message
        self.message_id = message.id
        self.is_ephemeral = is_ephemeral
        
        # Configure custom_id for persistent views to enable button functionality after bot restarts
        if self.persistent and not is_ephemeral:
            self._setup_button_ids()
            try:
                await message.edit(view=self)
                bot.add_view(self, message_id=self.message_id)
            except Exception as e:
                print(f"Error setting up persistent sticker view: {e}")
        
        # Only save to database if not ephemeral and persistent
        if self.persistent and not is_ephemeral:
            view_data = {
                'sticker_data': self.sticker_data,
                'original_size': self.original_size
            }
            await save_persistent_view(
                message.id,
                message.channel.id,
                'sticker_size',
                view_data,
                None  # Never expires for persistent views
            )
        
    @discord.ui.button(label='48px', style=discord.ButtonStyle.secondary)
    async def size_48(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 48)
        
    @discord.ui.button(label='128px', style=discord.ButtonStyle.secondary)
    async def size_128(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 128)
        
    @discord.ui.button(label='160px', style=discord.ButtonStyle.secondary)
    async def size_160(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 160)
        
    @discord.ui.button(label='240px', style=discord.ButtonStyle.secondary)
    async def size_240(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 240)
        
    @discord.ui.button(label='320px', style=discord.ButtonStyle.secondary)
    async def size_320(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_size(interaction, 320)
    
    async def update_size(self, interaction: discord.Interaction, new_size: int):
        # Check if view is already stopped or buttons are disabled
        if self.is_finished() or any(item.disabled for item in self.children if isinstance(item, discord.ui.Button)):
            return
        
        # Defer to show loading state
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass  # Already responded
        
        # Check expiration for ephemeral messages only
        if self.is_ephemeral and self.expires_at and int(time.time()) > self.expires_at:
            await notify_view_expired(interaction, self)
            return
        
        # Validate size is in STICKER_SIZES
        if new_size not in STICKER_SIZES:
            embed = discord.Embed(
                title="Invalid size",
                description=f"The size `{new_size}px` is not supported for stickers.",
                color=0xED4245
            )
            embed.add_field(
                name="Supported sizes",
                value=", ".join(f"{size}px" for size in STICKER_SIZES),
                inline=False
            )
            try:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            except:
                try:
                    await interaction.followup.send(embed=embed, ephemeral=True)
                except:
                    pass
            return
        
        # Use backend URL if available, otherwise use Discord CDN with size parameter
        if self.uses_backend and self.backend_urls and new_size in self.backend_urls:
            new_url = self.backend_urls[new_size]
        elif self.sticker_data.get('is_gif', False):
            # For direct GIF URLs, modify size parameter
            base_url = self.sticker_data['url']
            # Remove existing size parameter
            base_url = re.sub(r'[?&]size=\d+', '', base_url)
            # Add new size parameter
            if '?' in base_url:
                new_url = f'{base_url}&size={new_size}'
            else:
                new_url = f'{base_url}?size={new_size}'
        elif self.base_url:
            new_url = get_emoji_size(self.base_url, new_size)
        else:
            new_url = self.sticker_data['url']
        
        updated_data = self.sticker_data.copy()
        updated_data['url'] = new_url
        
        embed = build_single_embed(updated_data, new_size, self.expires_at)
        # Ensure thumbnail is set with the correctly sized URL
        embed.set_thumbnail(url=new_url)

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.style = discord.ButtonStyle.primary if f"{new_size}px" in item.label else discord.ButtonStyle.secondary

        try:
            await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)
        except:
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except:
                pass

    async def on_timeout(self) -> None:
        """Disable all buttons when the view times out after 5 minutes.
        
        Note: Stickers are stored permanently in the backend channel and never deleted.
        """
        try:
            disable_view_components(self)
            # Update the message to show disabled buttons
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.errors.NotFound:
                    # Message was deleted, ignore
                    pass
                except Exception as e:
                    print(f"Error editing message on timeout: {e}")
        except Exception as e:
            print(f"Error in on_timeout: {e}")

class BatchSizeView(discord.ui.View):
    def __init__(self, emojis_data, expires_at: Optional[int] = None, persistent: bool = False, is_ephemeral: bool = False):
        # Ephemeral messages expire after 5 minutes, persistent ones never expire
        if is_ephemeral:
            super().__init__(timeout=300)
            self.expires_at = expires_at if expires_at else (int(time.time()) + 300)
            self.persistent = False
        else:
            super().__init__(timeout=None if persistent else 300)  # None = never expires
            self.expires_at = None if persistent else expires_at
            self.persistent = persistent
        
        self.emojis_data = emojis_data
        self.is_ephemeral = is_ephemeral
        self.message: Optional[discord.Message] = None
        self.message_id: Optional[int] = None  # Set when message is sent (needed for custom_id)
        # Store base URLs without size parameters for consistent size changes
        self.base_emojis = []
        for emoji in emojis_data:
            base_emoji = emoji.copy()
            base_emoji['url'] = remove_size_param(emoji['url'])
            if 'gif_url' in emoji:
                base_emoji['gif_url'] = remove_size_param(emoji['gif_url'])
            self.base_emojis.append(base_emoji)
    
    def _generate_view_id(self) -> str:
        """Generate a unique view ID based on emojis data (used for custom_id)."""
        if self.emojis_data:
            first_emoji_id = self.emojis_data[0].get('id') or str(abs(hash(self.emojis_data[0].get('url', ''))))[:10]
            return f"batch_{first_emoji_id}_{len(self.emojis_data)}"
        return f"batch_{abs(hash(str(self.emojis_data)))}"
    
    def _setup_select_id(self):
        """Set custom_id for select menu based on message_id."""
        if not self.persistent or not self.message_id:
            return
        
        view_id = self._generate_view_id()
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                item.custom_id = f"{view_id}_select_{self.message_id}"
    
    async def on_message_set(self, message: discord.Message, is_ephemeral: bool = False) -> None:
        """Called after message is sent - save to database for persistence if not ephemeral."""
        self.message = message
        self.message_id = message.id
        self.is_ephemeral = is_ephemeral
        
        # Configure custom_id for persistent views to enable menu functionality after bot restarts
        if self.persistent and not is_ephemeral:
            self._setup_select_id()
            try:
                await message.edit(view=self)
                bot.add_view(self, message_id=self.message_id)
            except Exception as e:
                print(f"Error setting up persistent batch view: {e}")
        
        # Only save to database if not ephemeral and persistent
        if self.persistent and not is_ephemeral:
            view_data = {
                'emojis_data': self.emojis_data,
            }
            await save_persistent_view(
                message.id,
                message.channel.id,
                'batch_size',
                view_data,
                None  # Never expires for persistent views
            )
        
    @discord.ui.select(
        placeholder="Select a size for every link…",
        options=[
            discord.SelectOption(label="Original", value="original"),
            discord.SelectOption(label="24px", value="24"),
            discord.SelectOption(label="48px", value="48", default=True),
            discord.SelectOption(label="56px", value="56"),
            discord.SelectOption(label="128px", value="128"),
        ]
    )
    async def size_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        # Check if view is already stopped or UI elements are disabled
        if self.is_finished() or any(getattr(item, 'disabled', False) for item in self.children):
            return
        
        # Defer to show loading state
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass  # Already responded
        
        # Check expiration for ephemeral messages only
        if self.is_ephemeral and self.expires_at and int(time.time()) > self.expires_at:
            await notify_view_expired(interaction, self)
            return
            
        chosen = select.values[0]
        is_original = (chosen == 'original')
        new_size = int(chosen) if not is_original else None
        
        # Always use base URLs and apply the selected size
        updated_emojis = []
        for base_emoji in self.base_emojis:
            updated_emoji = base_emoji.copy()
            if is_original:
                updated_emoji['url'] = base_emoji['url']  # Already has size removed
            else:
                updated_emoji['url'] = get_emoji_size(base_emoji['url'], new_size)
            
            if 'gif_url' in base_emoji:
                if is_original:
                    updated_emoji['gif_url'] = base_emoji['gif_url']
                else:
                    updated_emoji['gif_url'] = get_emoji_size(base_emoji['gif_url'], new_size)
            updated_emojis.append(updated_emoji)
        
        embed = build_batch_embed(updated_emojis, 'original' if is_original else new_size, self.expires_at)

        for option in select.options:
            option.default = (option.value == chosen)
        
        try:
            await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)
        except:
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except:
                pass

    async def on_timeout(self) -> None:
        """Disable all UI elements when the view times out after 5 minutes."""
        try:
            disable_view_components(self)
            # Update the message to show disabled elements
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.errors.NotFound:
                    # Message was deleted, ignore
                    pass
                except Exception as e:
                    print(f"Error editing message on timeout: {e}")
        except Exception as e:
            print(f"Error in on_timeout: {e}")

class GuildSyncModal(discord.ui.Modal, title="Sync Commands to Server"):
    guild_id = discord.ui.TextInput(
        label="Server ID",
        placeholder="Enter the Guild ID to sync...",
        required=True,
        min_length=15,
        max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_obj = discord.Object(id=int(self.guild_id.value))
            bot.tree.copy_global_to(guild=guild_obj)
            await bot.tree.sync(guild=guild_obj)
            await interaction.followup.send(f"✅ Successfully synced commands to guild `{self.guild_id.value}`.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to sync: {e}", ephemeral=True)

class AdminControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Sync Globally", style=discord.ButtonStyle.danger, custom_id="admin_sync_global")
    async def sync_global(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Double check owner
        if not await bot.is_owner(interaction.user):
            await interaction.response.send_message("You are not authorized.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        try:
            synced = await bot.tree.sync()
            await interaction.followup.send(f"✅ Synced {len(synced)} global commands.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Global sync failed: {e}", ephemeral=True)

    @discord.ui.button(label="Sync Specific Server", style=discord.ButtonStyle.primary, custom_id="admin_sync_guild")
    async def sync_guild(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await bot.is_owner(interaction.user):
            await interaction.response.send_message("You are not authorized.", ephemeral=True)
            return
        await interaction.response.send_modal(GuildSyncModal())

def build_single_embed(data: dict, size, expires_at: Optional[int] = None) -> discord.Embed:
    """Build a Discord embed for a single emoji CDN link.
    
    Args:
        data: Emoji data dictionary
        size: Size value (int or 'original')
        expires_at: Optional expiry timestamp
    
    Returns:
        Discord embed object
    """
    type_display = {
        'discord_emoji': 'Custom Emoji',
        'raw_id': 'Emoji ID',
        'url': 'Direct URL',
        'sticker_url': 'Sticker'
    }.get(data['type'], 'Unknown')
    
    # For Lottie stickers, don't show size or expiration (no buttons, no size control)
    is_lottie = data.get('type') in ('sticker_url', 'sticker_id') and data.get('is_lottie')
    
    if is_lottie:
        description = f"Source: **{type_display}**"
    else:
        size_label = 'Original' if size == 'original' else f"{size}px"
        description = f"Source: **{type_display}** · Size `{size_label}`"
    
    if expires_at:
            description += f"\nExpires <t:{expires_at}:R>"
    
    embed = discord.Embed(
        title="CDN delivery link",
        description=description,
        color=0x2F3136
    )
    
    # Primary CDN URL field - make it prominent
    # Format URL display: show beginning and end if too long
    url_display = data['url']
    if len(url_display) > 80:
        # Show first 50 chars, ..., last 20 chars
        url_display = f"{url_display[:50]}...{url_display[-20:]}"
    embed.add_field(
        name="CDN URL", 
        value=f"[Open link in browser]({data['url']})\n`{url_display}`", 
        inline=False
    )
    
    # Metadata fields in a row
    metadata_fields = []
    
    # Handle name/ID display differently for stickers vs emojis
    if data.get('name') and data['type'] in ('discord_emoji', 'raw_id'):
        # Emojis with name: show copiable :name: format
        metadata_fields.append(("Name", f"`:{data['name']}:`"))
    elif data.get('id'):
        if data['type'] in ('sticker_url', 'sticker_id'):
            # Stickers: show ID only (no :name: format)
            metadata_fields.append(("Sticker ID", f"`{data['id']}`"))
        else:
            # Emojis without name: show ID
            metadata_fields.append(("ID", f"`{data['id']}`"))
    
    # Format field
    if data.get('animated'):
        if data.get('type') in ('sticker_url', 'sticker_id'):
            # Check if it's a Lottie sticker
            if data.get('is_lottie'):
                format_name = "Lottie (JSON)"
            else:
                # Determine sticker format type for display
                format_name = "APNG"
                if 'gif' in data.get('url', ''):
                    format_name = "GIF"
            metadata_fields.append(("Format", f"Animated Sticker ({format_name})"))
        else:
            metadata_fields.append(("Format", "Animated GIF"))
    elif data.get('type') == 'discord_emoji':
        metadata_fields.append(("Format", "Static WebP"))
    elif data.get('type') in ('sticker_url', 'sticker_id'):
        metadata_fields.append(("Format", "Static Sticker (WebP)"))
    
    # Add metadata fields in a row (up to 3 inline)
    for i, (name, value) in enumerate(metadata_fields[:3]):
        embed.add_field(name=name, value=value, inline=True)
            
            # Show processing info only for stickers that were converted
    # Emojis don't need any messages - they work fine
    if data.get('type') in ('sticker_url', 'sticker_id'):
        if data.get('is_lottie') and not data.get('uses_backend'):
            # Lottie stickers are not supported - show manual conversion link
                embed.add_field(
                name="Lottie format not supported",
                value="Lottie animations are not automatically converted due to high server load. [Convert manually on LottieFiles](https://lottiefiles.com/tools/lottie-to-gif) and use the JSON link above.",
                    inline=False
                )
        elif data.get('uses_backend'):
            if data.get('animated') and not data.get('is_gif'):
                # Animated APNG stickers were converted because Discord doesn't support animated APNG embeds
                    embed.add_field(
                    name="Format conversion",
                    value="Discord embeds do not support animated APNG. Converted to GIF format for compatibility.",
                        inline=False
                    )
    
    embed.set_thumbnail(url=data['url'])
    
    # Only show footer about buttons if it's not a Lottie sticker (Lottie stickers don't have size buttons)
    if not (data.get('type') in ('sticker_url', 'sticker_id') and data.get('is_lottie')):
        embed.set_footer(text="Use the buttons below to request another size.")
    
    return embed

SESSION_EXPIRED_COLOR = 0x5865F2

def build_session_expired_embed() -> discord.Embed:
    """Create a consistent notice when interactive menus expire."""
    embed = discord.Embed(
        title="Session expired",
        description="This interactive menu is no longer active. Run the command again to generate a fresh panel.",
        color=SESSION_EXPIRED_COLOR
    )
    embed.add_field(
        name="Why did it close?",
        value="Ephemeral menus automatically end after five minutes to keep conversations tidy.",
        inline=False
    )
    embed.set_footer(text="Persistent messages stay active until you dismiss them.")
    return embed

def disable_view_components(view: discord.ui.View) -> None:
    """Disable all actionable components in a view."""
    for item in getattr(view, 'children', []):
        if hasattr(item, 'disabled'):
            item.disabled = True
    view.stop()

async def notify_view_expired(interaction: discord.Interaction, view: Optional[discord.ui.View] = None) -> None:
    """Send a professional notice after an interaction has expired."""
    embed = build_session_expired_embed()
    responded = interaction.response.is_done()
    if view:
        disable_view_components(view)
        try:
            await interaction.response.edit_message(view=view)
            responded = True
        except discord.InteractionResponded:
            responded = True
        except Exception:
            pass
    if responded:
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            pass
        return
    try:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.InteractionResponded:
        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            pass
    except Exception:
        pass

def build_batch_embed(items: list, size, expires_at: Optional[int] = None) -> discord.Embed:
    """Build a Discord embed for multiple emoji CDN links.
    
    Args:
        items: List of emoji data dictionaries
        size: Size value (int or 'original')
        expires_at: Optional expiry timestamp
    
    Returns:
        Discord embed object
    """
    size_label = 'Original' if size == 'original' else f"{size}px"
    embed = discord.Embed(
        title=f"Batch CDN Links · {len(items)} item{'s' if len(items) != 1 else ''}",
        description=f"Size: `{size_label}`",
        color=0x2F3136
    )
    
    if expires_at:
        embed.description += f"\nExpires <t:{expires_at}:R>"
    
    links_text = []
    for i, emoji in enumerate(items, 1):
        name_display = f":{emoji.get('name')}:" if emoji.get('name') else emoji.get('id', 'Unknown')
        # Make links more visually distinct
        links_text.append(f"**{i}.** {name_display}\n└ [Copy CDN Link]({emoji['url']})")
        
        if emoji.get('type') == 'raw_id' and 'gif_url' in emoji:
            if size == 'original':
                gif_url_sized = remove_size_param(emoji['gif_url'])
            else:
                gif_url_sized = get_emoji_size(emoji['gif_url'], int(size))
            links_text.append(f"└ [GIF URL]({gif_url_sized})")
    
    # Split into chunks if needed (Discord field value limit is 1024)
    full_text = '\n\n'.join(links_text)
    if len(full_text) > 1000:
        # Split roughly in half
        mid_point = len(links_text) // 2
        first_half = '\n\n'.join(links_text[:mid_point])
        second_half = '\n\n'.join(links_text[mid_point:])
        
        embed.add_field(
            name=f"Links (1–{mid_point})", 
            value=first_half, 
            inline=False
        )
        embed.add_field(
            name=f"Links ({mid_point + 1}–{len(links_text)})", 
            value=second_half, 
            inline=False
        )
    else:
        embed.add_field(name="CDN links", value=full_text, inline=False)
    
    embed.set_footer(text="Use the selector below to change size for all items.")
    
    return embed

def parse_discord_emoji(input_text: str, animated: Optional[bool] = None) -> Optional[dict]:
    """Parse Discord emoji input into structured data.
    
    Supports:
    - Discord emoji format: <:name:id> or <a:name:id>
    - Raw emoji ID: 123456789012345678
    - Direct URL: https://...
    
    Args:
        input_text: The emoji input to parse
        animated: Optional explicit animation flag for raw IDs (True/False)
    
    Returns dict with emoji data or None if invalid.
    """
    if not input_text:
        return None
    
    input_text = input_text.strip()
    
    if len(input_text) > 200:
        return None
    
    if input_text.startswith(('http://', 'https://', 'data:')):
        if not re.match(r'^https?://.+\..+', input_text):
            return None
        return {
            'url': input_text,
            'type': 'url',
            'animated': False,
            'id': None,
            'name': None
        }
    
    emoji_match = re.match(r'<(a)?:(\w+):(\d{15,20})>', input_text)
    if emoji_match:
        animated = bool(emoji_match.group(1))
        name = emoji_match.group(2)
        emoji_id = emoji_match.group(3)
        
        if not re.match(r'^[a-zA-Z0-9_]{1,32}$', name):
            return None
            
        ext = 'gif' if animated else 'webp'
        url = f'https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=128&quality=lossless'
        
        return {
            'url': url,
            'type': 'discord_emoji',
            'animated': animated,
            'id': emoji_id,
            'name': name
        }
    
    id_match = re.match(r'^\d{15,20}$', input_text)
    if id_match:
        emoji_id = id_match.group(0)
        # Use animated parameter if provided, otherwise default to None (unknown)
        is_animated = animated if animated is not None else None
        
        if is_animated is True:
            # Explicitly animated - use GIF URL
            url = f'https://cdn.discordapp.com/emojis/{emoji_id}.gif?size=128&quality=lossless'
            return {
                'url': url,
                'type': 'raw_id',
                'animated': True,
                'id': emoji_id,
                'name': None,
                'gif_url': url
            }
        elif is_animated is False:
            # Explicitly static - use WebP URL only
            webp_url = f'https://cdn.discordapp.com/emojis/{emoji_id}.webp?size=128&quality=lossless'
            return {
                'url': webp_url,
                'type': 'raw_id',
                'animated': False,
                'id': emoji_id,
                'name': None
            }
        else:
            # Unknown - provide both URLs
            webp_url = f'https://cdn.discordapp.com/emojis/{emoji_id}.webp?size=128&quality=lossless'
        return {
            'url': webp_url,
            'type': 'raw_id',
            'animated': None,
            'id': emoji_id,
            'name': None,
            'gif_url': f'https://cdn.discordapp.com/emojis/{emoji_id}.gif?size=128&quality=lossless'
        }
    
    return None

def parse_discord_sticker(input_text: str, animated: Optional[bool] = None) -> Optional[dict]:
    """Parse Discord sticker input into structured data.
    
    Supports:
    - Sticker URL: https://media.discordapp.net/stickers/{id}.webp?...
    - Raw sticker ID: 123456789012345678 (15-20 digits)
    
    Detects animated parameter in URLs and preserves it.
    
    Args:
        input_text: The sticker input to parse
        animated: Optional explicit animation flag for raw IDs (True/False)
    
    Returns dict with sticker data or None if invalid.
    """
    if not input_text:
        return None
    
    input_text = input_text.strip()
    
    if len(input_text) > 200:
        return None
    
    # Check for sticker URL pattern (media.discordapp.net)
    sticker_url_match = re.match(
        r'https?://media\.discordapp\.net/stickers/(\d{15,20})\.webp',
        input_text
    )
    if sticker_url_match:
        sticker_id = sticker_url_match.group(1)
        # Check if URL already has animated=true parameter
        has_animated = 'animated=true' in input_text or '&animated=true' in input_text
        # Build URL preserving animated parameter if present
        if has_animated:
            url = f'https://media.discordapp.net/stickers/{sticker_id}.webp?size={STICKER_DEFAULT_SIZE}&quality=lossless&animated=true'
        else:
            url = f'https://media.discordapp.net/stickers/{sticker_id}.webp?size={STICKER_DEFAULT_SIZE}&quality=lossless'
        return {
            'url': url,
            'type': 'sticker_url',
            'id': sticker_id,
            'name': None,
            'animated': has_animated
        }
    
    # Check for cdn.discordapp.com sticker URL pattern (PNG format)
    # These need to be normalized to media.discordapp.net for non-animated stickers
    cdn_sticker_match = re.match(
        r'https?://cdn\.discordapp\.com/stickers/(\d{15,20})\.png',
        input_text
    )
    if cdn_sticker_match:
        sticker_id = cdn_sticker_match.group(1)
        # Assume non-animated (animated stickers typically use .json or have animated=true)
        # Normalize to media.discordapp.net for proper size parameter support
        url = f'https://media.discordapp.net/stickers/{sticker_id}.png?size={STICKER_DEFAULT_SIZE}'
        return {
            'url': url,
            'type': 'sticker_url',
            'id': sticker_id,
            'name': None,
            'animated': False
        }
    
    # Check for raw sticker ID (15-20 digits)
    # This pattern may match emoji IDs as well; sticker parsing is attempted first in unified parser
    id_match = re.match(r'^\d{15,20}$', input_text)
    if id_match:
        sticker_id = id_match.group(0)
        # Use animated parameter if provided, otherwise default to False
        is_animated = animated if animated is not None else False
        
        if is_animated:
            # Animated sticker - use animated=true parameter
            url = f'https://media.discordapp.net/stickers/{sticker_id}.webp?size={STICKER_DEFAULT_SIZE}&quality=lossless&animated=true'
        else:
            # Static sticker
            url = f'https://media.discordapp.net/stickers/{sticker_id}.webp?size={STICKER_DEFAULT_SIZE}&quality=lossless'
        
        return {
            'url': url,
            'type': 'sticker_id',
            'id': sticker_id,
            'name': None,
            'animated': is_animated
        }
    
    return None

def parse_discord_asset(input_text: str, animated: Optional[bool] = None) -> Optional[dict]:
    """Parse Discord asset (emoji or sticker) input.
    Tries emoji first, then sticker.
    
    Args:
        input_text: The asset input to parse
        animated: Optional explicit animation flag for raw IDs (True/False)
    
    Returns dict with asset data or None if invalid.
    """
    # Try emoji first (handles <:name:id> and raw IDs)
    result = parse_discord_emoji(input_text, animated=animated)
    if result:
        return result
    
    # Try sticker (only handles URLs)
    result = parse_discord_sticker(input_text, animated=animated)
    if result:
        return result
    
    return None

def normalize_sticker_url(url: str, is_animated: bool = False) -> str:
    """Normalize sticker URL to use media.discordapp.net for non-animated PNG stickers.
    
    For non-animated PNG stickers, cdn.discordapp.com doesn't respect size parameters.
    Convert to media.discordapp.net/stickers/{id}.png which works correctly.
    
    Args:
        url: Sticker URL (may use cdn.discordapp.com or media.discordapp.net)
        is_animated: Whether the sticker is animated
    
    Returns:
        Normalized URL using media.discordapp.net for non-animated PNG stickers
    """
    # Only convert non-animated PNG stickers from cdn.discordapp.com to media.discordapp.net
    if not is_animated and 'cdn.discordapp.com/stickers' in url and '.png' in url:
        # Extract sticker ID from URL
        match = re.search(r'cdn\.discordapp\.com/stickers/(\d{15,20})\.png', url)
        if match:
            sticker_id = match.group(1)
            # Preserve query parameters if any
            query_part = ''
            if '?' in url:
                query_part = '?' + url.split('?', 1)[1]
            # Convert to media.discordapp.net format
            return f'https://media.discordapp.net/stickers/{sticker_id}.png{query_part}'
    return url

def get_emoji_size(base_url: str, size: int = 128) -> str:
    """Add or update size parameter in emoji/sticker CDN URL.
    
    Preserves animated parameter for stickers and normalizes non-animated PNG sticker URLs.
    
    Args:
        base_url: The emoji/sticker CDN URL
        size: Target size. For emojis: 16, 24, 32, 48, 56, 64, 128. For stickers: 24, 48, 96, 160, 240, 320.
    
    Returns:
        URL with size parameter added/updated, preserving animated parameter if present
    """
    # Check if this is a sticker URL (both cdn.discordapp.com and media.discordapp.net)
    is_sticker = 'cdn.discordapp.com/stickers' in base_url or 'media.discordapp.net/stickers' in base_url
    is_cdn_sticker = 'cdn.discordapp.com/stickers' in base_url  # Discord's CDN format (works with size for animated)
    has_animated = 'animated=true' in base_url
    
    # Normalize non-animated PNG sticker URLs to media.discordapp.net
    if is_sticker and not has_animated and '.png' in base_url:
        base_url = normalize_sticker_url(base_url, is_animated=False)
    
    # Determine valid sizes based on URL type
    if is_sticker:
        valid_sizes = STICKER_SIZES
        target_size = size if size in valid_sizes else STICKER_DEFAULT_SIZE
    else:
        valid_sizes = [16, 24, 32, 48, 56, 64, 128]
        target_size = size if size in valid_sizes else 128
    
    # Update or add size parameter
    # For cdn.discordapp.com stickers, size parameter works for both animated and static
    # For media.discordapp.net stickers, size parameter works for static (.webp)
    if '?size=' in base_url:
        base_url = re.sub(r'size=\d+', f'size={target_size}', base_url)
    elif '&size=' in base_url:
        base_url = re.sub(r'&size=\d+', f'&size={target_size}', base_url)
    else:
        separator = '&' if '?' in base_url else '?'
        base_url += f'{separator}size={target_size}'
    
    # For stickers, ensure animated parameter is preserved if it was present
    if is_sticker and has_animated and 'animated=true' not in base_url:
        # Add animated parameter if it was removed during size update
        separator = '&' if '?' in base_url else '?'
        base_url += f'{separator}animated=true'
    
    return base_url

def remove_size_param(base_url: str) -> str:
    """Remove size parameter from URL query string while preserving other parameters (including animated)"""
    if 'size=' not in base_url:
        return base_url
    
    # Check if size is the first parameter (after ?) or a later parameter (after &)
    if '?size=' in base_url:
        # Size is first parameter: remove ?size=XXX and replace with ? if there are other params
        # Pattern: ?size=XXX&other or ?size=XXX
        base_url = re.sub(r'\?size=\d+(&)?', lambda m: '?' if m.group(1) else '', base_url)
    elif '&size=' in base_url:
        # Size is a later parameter: remove &size=XXX
        base_url = re.sub(r'&size=\d+', '', base_url)
    
    # Clean up any trailing ? or & if no parameters remain (but preserve animated if it exists)
    # Only remove trailing ? or & if there are no other parameters
    if '?' in base_url:
        # Check if there are any parameters left after removing size
        query_part = base_url.split('?', 1)[1] if '?' in base_url else ''
        if query_part and not any(c in query_part for c in ['=', '&']):
            # Only ? or & left, remove it
            base_url = re.sub(r'[?&]+$', '', base_url)
    
    return base_url

# Backend server functions (Discord guild as storage)
# Cache to store sticker_id -> URLs mapping to avoid re-uploading
# Uses hybrid approach: Local SQLite (primary) + Firebase (backup only)
# Strategy: Read from local first, write to both local and Firebase

# Local SQLite database (primary)
LOCAL_DB_FILE = 'sticker_cache.db'
PERSISTENT_DB_FILE = 'persistent_views.db'
USER_SAVED_ITEMS_DB = 'user_saved_items.db'

# Firebase Firestore client (backup only - writes only)
firestore_db: Optional[firestore.Client] = None

async def init_local_database() -> None:
    """Initialize the local SQLite database (primary storage)."""
    # Sticker cache database
    async with aiosqlite.connect(LOCAL_DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sticker_cache (
                sticker_id TEXT NOT NULL,
                size INTEGER NOT NULL,
                url TEXT NOT NULL,
                message_id TEXT,
                created_at REAL NOT NULL,
                PRIMARY KEY (sticker_id, size)
            )
        ''')
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_sticker_id ON sticker_cache(sticker_id)
        ''')
        await db.commit()
    print(f"Sticker cache database initialized: {LOCAL_DB_FILE}")
    
    # Persistent views database
    async with aiosqlite.connect(PERSISTENT_DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS persistent_views (
                message_id TEXT NOT NULL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                view_type TEXT NOT NULL,
                view_data TEXT NOT NULL,
                expires_at REAL,
                created_at REAL NOT NULL
            )
        ''')
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_expires_at ON persistent_views(expires_at)
        ''')
        await db.commit()
    print(f"Persistent views database initialized: {PERSISTENT_DB_FILE}")
    
    # User saved items database
    async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_saved_items (
                user_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                item_type TEXT NOT NULL,
                item_format TEXT,
                item_data TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (user_id, item_name, item_type)
            )
        ''')
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_items ON user_saved_items(user_id, item_type)
        ''')
        await db.commit()
    print(f"User saved items database initialized: {USER_SAVED_ITEMS_DB}")

def init_firestore() -> None:
    """Initialize Firebase Firestore database (backup only - writes only).
    
    Requires either:
    - FIREBASE_CREDENTIALS_PATH pointing to a service account JSON file, OR
    - FIREBASE_PROJECT_ID with credentials set via environment (e.g., GOOGLE_APPLICATION_CREDENTIALS)
    """
    global firestore_db
    
    if not FIREBASE_AVAILABLE:
        print("Firebase Admin SDK not available. Backup sync will not work.")
        return
    
    try:
        # Check if Firebase app is already initialized
        try:
            firebase_admin.get_app()
            print("Firebase already initialized")
        except ValueError:
            # Initialize Firebase application
            if os.path.exists(FIREBASE_CREDENTIALS_PATH):
                # Use credentials file
                cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
                firebase_admin.initialize_app(cred)
                print(f"Firebase initialized with credentials from {FIREBASE_CREDENTIALS_PATH}")
            elif FIREBASE_PROJECT_ID:
                # Use default credentials for cloud environments
                firebase_admin.initialize_app(options={'projectId': FIREBASE_PROJECT_ID})
                print(f"Firebase initialized with project ID: {FIREBASE_PROJECT_ID}")
            else:
                # Use default initialization via GOOGLE_APPLICATION_CREDENTIALS environment variable
                firebase_admin.initialize_app()
                print("Firebase initialized with default credentials")
        
        # Initialize Firestore client
        firestore_db = firestore.client()
        print("Firebase Firestore connected (backup mode - writes only)")
        
    except Exception as e:
        print(f"Failed to initialize Firebase: {e}")
        print("Backup sync will not work. Continuing without Firebase...")
        firestore_db = None

# User saved items database functions
async def save_user_item(user_id: str, item_name: str, item_type: str, item_data: dict, item_format: Optional[str] = None) -> str:
    """Save emoji/sticker to user's personal list.
    
    Args:
        user_id: Discord user ID
        item_name: Display name for the item
        item_type: 'emoji' or 'sticker'
        item_data: Dictionary containing item data (id, name, url, animated, type, etc.)
        item_format: Optional format type for stickers (lottie, apng, png, gif, webp)
    
    Returns:
        Final name used (may be modified if duplicate)
    """
    final_name = item_name
    # Check if name exists and handle duplicates
    count = await check_name_exists(user_id, item_name, item_type)
    if count > 0:
        final_name = f"{item_name}{count}"
    
    try:
        async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
            await db.execute('''
                INSERT OR REPLACE INTO user_saved_items 
                (user_id, item_name, item_type, item_format, item_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                str(user_id),
                final_name,
                item_type,
                item_format,
                json.dumps(item_data),
                time.time()
            ))
            await db.commit()
    except Exception as e:
        print(f"Error saving user item: {e}")
    
    # Backup to Firebase (async, don't wait for it)
    asyncio.create_task(save_user_item_firebase_backup(user_id, final_name, item_type, item_data, item_format))
    
    return final_name

async def save_user_item_firebase_backup(user_id: str, item_name: str, item_type: str, item_data: dict, item_format: Optional[str] = None) -> None:
    """Backup user saved item to Firebase (writes only)."""
    if not firestore_db:
        return
    
    try:
        # Ensure user_id and item_name are valid non-empty strings
        user_id_str = str(user_id).strip()
        item_name_str = str(item_name).strip()
        item_type_str = str(item_type).strip()
        
        if not user_id_str or not item_name_str or not item_type_str:
            print(f"Invalid IDs for Firebase backup: user_id={user_id}, item_name={item_name}, item_type={item_type}")
            return
        
        # Clean and serialize item_data for Firestore
        cleaned_item_data = {}
        for key, value in item_data.items():
            if value is not None:
                key_str = str(key).strip()
                if not key_str:
                    continue  # Skip empty keys
                # Convert to JSON string for complex nested structures
                if isinstance(value, (dict, list)):
                    try:
                        serialized = json.dumps(value, ensure_ascii=False)
                        if serialized:  # Only add if not empty
                            cleaned_item_data[key_str] = serialized
                    except (TypeError, ValueError) as e:
                        # If can't serialize, skip it
                        print(f"Could not serialize {key_str} for Firebase: {e}")
                        continue
                elif isinstance(value, (str, int, float, bool)):
                    # Only add non-empty strings
                    if isinstance(value, str) and not value.strip():
                        continue
                    cleaned_item_data[key_str] = value
                else:
                    # Convert other types to string, but skip if empty
                    value_str = str(value).strip()
                    if value_str:
                        cleaned_item_data[key_str] = value_str
        
        # Use composite document ID: user_id/item_name/item_type
        doc_id = f"{user_id_str}_{item_name_str}_{item_type_str}"
        doc_ref = firestore_db.collection('user_saved_items').document(doc_id)
        
        # Build document data, only including non-None values
        doc_data = {
            'user_id': user_id_str,
            'item_name': item_name_str,
            'item_type': item_type_str,
            'item_data': cleaned_item_data,
            'created_at': firestore.SERVER_TIMESTAMP,
            'updated_at': firestore.SERVER_TIMESTAMP
        }
        
        # Only add item_format if it's not None
        if item_format is not None:
            doc_data['item_format'] = str(item_format).strip()
        
        doc_ref.set(doc_data, merge=True)
    except Exception as e:
        print(f"Error backing up user saved item to Firebase: {e}")
        import traceback
        traceback.print_exc()

async def get_user_item_by_name(user_id: str, item_name: str, item_type: str) -> Optional[dict]:
    """Get item by name and type from user's list.
    
    Args:
        user_id: Discord user ID
        item_name: Item display name
        item_type: 'emoji' or 'sticker'
    
    Returns:
        Dictionary with item data or None if not found
    """
    try:
        async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
            async with db.execute('''
                SELECT item_data, item_format FROM user_saved_items
                WHERE user_id = ? AND item_name = ? AND item_type = ?
            ''', (str(user_id), item_name, item_type)) as cursor:
                row = await cursor.fetchone()
                if row:
                    item_data = json.loads(row[0])
                    if row[1]:  # item_format
                        item_data['format'] = row[1]
                    return item_data
    except Exception as e:
        print(f"Error getting user item: {e}")
    return None

async def get_user_items(user_id: str, item_type: Optional[str] = None) -> list:
    """Get all items from user's list, optionally filtered by type.
    
    Args:
        user_id: Discord user ID
        item_type: Optional filter ('emoji' or 'sticker')
    
    Returns:
        List of dictionaries with item_name and item_data
    """
    items = []
    try:
        async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
            if item_type:
                async with db.execute('''
                    SELECT item_name, item_data, item_format FROM user_saved_items
                    WHERE user_id = ? AND item_type = ?
                    ORDER BY created_at DESC
                ''', (str(user_id), item_type)) as cursor:
                    rows = await cursor.fetchall()
            else:
                async with db.execute('''
                    SELECT item_name, item_data, item_format FROM user_saved_items
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                ''', (str(user_id),)) as cursor:
                    rows = await cursor.fetchall()
            
            for row in rows:
                item_data = json.loads(row[1])
                if row[2]:  # item_format
                    item_data['format'] = row[2]
                items.append({
                    'name': row[0],
                    'data': item_data
                })
    except Exception as e:
        print(f"Error getting user items: {e}")
    return items

async def check_name_exists(user_id: str, item_name: str, item_type: str) -> int:
    """Check if name exists and return count for numbering.
    
    Args:
        user_id: Discord user ID
        item_name: Item display name
        item_type: 'emoji' or 'sticker'
    
    Returns:
        Count of existing items with similar names (for appending numbers)
    """
    try:
        async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
            # Check exact match
            async with db.execute('''
                SELECT COUNT(*) FROM user_saved_items
                WHERE user_id = ? AND item_name = ? AND item_type = ?
            ''', (str(user_id), item_name, item_type)) as cursor:
                exact_count = (await cursor.fetchone())[0]
            
            if exact_count == 0:
                return 0
            
            # Check numbered versions (name1, name2, etc.)
            max_num = 0
            async with db.execute('''
                SELECT item_name FROM user_saved_items
                WHERE user_id = ? AND item_type = ? AND item_name LIKE ?
            ''', (str(user_id), item_type, f"{item_name}%")) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    name = row[0]
                    if name == item_name:
                        continue
                    # Check if it's name followed by digits
                    if name.startswith(item_name):
                        suffix = name[len(item_name):]
                        if suffix.isdigit():
                            max_num = max(max_num, int(suffix))
            
            return max_num + 1
    except Exception as e:
        print(f"Error checking name exists: {e}")
    return 0

async def find_item_by_id(user_id: str, item_id: str, item_type: str) -> Optional[dict]:
    """Find an item by its ID (emoji ID or sticker ID) in user's list.
    
    Args:
        user_id: Discord user ID
        item_id: Emoji ID or sticker ID
        item_type: 'emoji' or 'sticker'
    
    Returns:
        Dictionary with item_name and item_data if found, None otherwise
    """
    try:
        async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
            async with db.execute('''
                SELECT item_name, item_data FROM user_saved_items
                WHERE user_id = ? AND item_type = ?
            ''', (str(user_id), item_type)) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    item_data = json.loads(row[1])
                    # Check if the ID matches
                    if item_data.get('id') == item_id:
                        return {
                            'name': row[0],
                            'data': item_data
                        }
    except Exception as e:
        print(f"Error finding item by ID: {e}")
    return None

async def remove_user_item(user_id: str, item_name: str, item_type: str) -> bool:
    """Remove item from user's list.
    
    Args:
        user_id: Discord user ID
        item_name: Item display name
        item_type: 'emoji' or 'sticker'
    
    Returns:
        True if removed, False if not found
    """
    try:
        async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
            async with db.execute('''
                DELETE FROM user_saved_items
                WHERE user_id = ? AND item_name = ? AND item_type = ?
            ''', (str(user_id), item_name, item_type)) as cursor:
                await db.commit()
                return cursor.rowcount > 0
    except Exception as e:
        print(f"Error removing user item: {e}")
    return False

async def get_cached_sticker_urls(sticker_id: str) -> Optional[Dict]:
    """Get cached sticker URLs - checks local SQLite first, then Firebase backup.
    
    Args:
        sticker_id: Sticker ID to look up
        
    Returns:
        Dictionary with 'urls' (size -> url) and 'message_ids' (list) or None if not found
    """
    # First, try local SQLite database (fast, no network)
    try:
        async with aiosqlite.connect(LOCAL_DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT size, url, message_id FROM sticker_cache WHERE sticker_id = ?',
                (sticker_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                
                if rows:
                    urls = {}
                    message_ids = []
                    message_ids_map = {}
                    
                    for row in rows:
                        size = row['size']
                        urls[size] = row['url']
                        if row['message_id']:
                            msg_id = row['message_id']
                            message_ids.append(msg_id)
                            message_ids_map[size] = msg_id
                    
                    return {
                        'urls': urls,
                        'message_ids': message_ids,
                        'message_ids_map': message_ids_map
                    }
    except Exception as e:
        print(f"Error reading from local database: {e}")
    
    # If not found locally, check Firebase backup (only if local DB fails)
    if firestore_db:
        try:
            doc_ref = firestore_db.collection('sticker_cache').document(sticker_id)
            doc = doc_ref.get()
            
            if doc.exists:
                data = doc.to_dict()
                if data:
                    urls_dict = data.get('urls', {})
                    message_ids_map = data.get('message_ids', {})
                    
                    urls = {int(k): v for k, v in urls_dict.items()}
                    message_ids_map_int = {int(k): v for k, v in message_ids_map.items()} if isinstance(message_ids_map, dict) else {}
                    message_ids_list = [msg_id for msg_id in message_ids_map_int.values() if msg_id]
                    
                    # Restore to local database for future fast access
                    if urls:
                        await cache_sticker_urls_local(sticker_id, urls, message_ids_map_int)
                    
                    return {
                        'urls': urls,
                        'message_ids': message_ids_list,
                        'message_ids_map': message_ids_map_int
                    }
        except Exception as e:
            print(f"Error reading from Firebase backup: {e}")
    
    return None

async def cache_sticker_urls_local(sticker_id: str, urls: Dict[int, str], message_ids_map: Dict[int, str]) -> None:
    """Cache sticker URLs in local SQLite database only (internal use)."""
    try:
        async with aiosqlite.connect(LOCAL_DB_FILE) as db:
            await db.execute('DELETE FROM sticker_cache WHERE sticker_id = ?', (sticker_id,))
            current_time = time.time()
            for size, url in urls.items():
                msg_id = message_ids_map.get(size)
                await db.execute(
                    'INSERT INTO sticker_cache (sticker_id, size, url, message_id, created_at) VALUES (?, ?, ?, ?, ?)',
                    (sticker_id, size, url, msg_id, current_time)
                )
            
            await db.commit()
    except Exception as e:
        print(f"Error writing to local database: {e}")

async def cache_sticker_urls_firebase_backup(sticker_id: str, urls: Dict[int, str], message_ids_map: Dict[int, str]) -> None:
    """Backup sticker URLs to Firebase (writes only, no reads)."""
    if not firestore_db:
        return
    
    try:
        doc_ref = firestore_db.collection('sticker_cache').document(sticker_id)
        
        urls_dict = {str(k): v for k, v in urls.items()}
        message_ids_dict = {str(k): str(v) for k, v in message_ids_map.items() if v}
        
        doc_ref.set({
            'urls': urls_dict,
            'message_ids': message_ids_dict,
            'updated_at': firestore.SERVER_TIMESTAMP,
            'sticker_id': sticker_id
        }, merge=True)
    except Exception as e:
        print(f"Error backing up to Firebase: {e}")

async def cache_sticker_urls(sticker_id: str, urls: Dict[int, str], message_ids_map: Optional[Dict[int, str]] = None) -> None:
    """Cache sticker URLs - writes to local SQLite (primary) and Firebase (backup).
    
    Args:
        sticker_id: Sticker ID
        urls: Dictionary mapping size -> URL
        message_ids_map: Optional dictionary mapping size -> message_id. If None, preserves existing from local DB.
    """
    # Get existing message_ids from local DB if not provided
    if message_ids_map is None:
        existing_data = await get_cached_sticker_urls(sticker_id)
        if existing_data:
            message_ids_map = existing_data.get('message_ids_map', {})
        else:
            message_ids_map = {}
    
    # Preserve existing message_ids for sizes we're keeping
    for size in urls.keys():
        if size not in message_ids_map:
            # Try to get from local DB
            try:
                async with aiosqlite.connect(LOCAL_DB_FILE) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute(
                        'SELECT message_id FROM sticker_cache WHERE sticker_id = ? AND size = ?',
                        (sticker_id, size)
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row and row['message_id']:
                            message_ids_map[size] = row['message_id']
            except:
                pass
    
    # Write to local SQLite (primary)
    await cache_sticker_urls_local(sticker_id, urls, message_ids_map)
    print(f"Cached {len(urls)} URLs for sticker {sticker_id} locally")
    
    # Backup to Firebase (async, don't wait for it)
    asyncio.create_task(cache_sticker_urls_firebase_backup(sticker_id, urls, message_ids_map))

async def update_cached_urls(sticker_id: str, urls: Dict[int, str]) -> None:
    """Update cached URLs for a sticker (removes expired/invalid URLs).
    Updates both local SQLite and Firebase backup.
    
    Args:
        sticker_id: Sticker ID
        urls: Dictionary mapping size -> URL (only valid URLs)
    """
    # Get existing message_ids from local DB to preserve them
    message_ids_map = {}
    try:
        async with aiosqlite.connect(LOCAL_DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                'SELECT size, message_id FROM sticker_cache WHERE sticker_id = ?',
                (sticker_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    size = row['size']
                    if size in urls and row['message_id']:  # Only keep if URL is still valid
                        message_ids_map[size] = row['message_id']
    except Exception as e:
        print(f"Error reading from local DB: {e}")
    
    # Update local SQLite
    await cache_sticker_urls_local(sticker_id, urls, message_ids_map)
    print(f"Updated local cache: {len(urls)} valid URLs for sticker {sticker_id}")
    
    # Update Firebase backup (async)
    asyncio.create_task(cache_sticker_urls_firebase_backup(sticker_id, urls, message_ids_map))

# Legacy cache variable for compatibility (not used directly, database is source of truth)
sticker_cache: Dict[str, Dict] = {}

# Shared HTTP session for better performance (reused across all requests)
_http_session: Optional[aiohttp.ClientSession] = None

def get_http_session() -> aiohttp.ClientSession:
    """Get or create shared HTTP session for better performance."""
    global _http_session
    if _http_session is None or _http_session.closed:
        # Create session with optimized settings for concurrent requests
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=5, connect=2)
        _http_session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={'User-Agent': 'Emolink-Bot/1.0'}
        )
    return _http_session

async def close_http_session():
    """Close the shared HTTP session."""
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()

async def validate_url(url: str) -> bool:
    """Validate that a URL is accessible via HTTP.
    
    Args:
        url: URL to validate
    
    Returns:
        True if URL is accessible (status 200), False otherwise
    """
    try:
        session = get_http_session()
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as response:
            return response.status == 200
    except Exception:
        # If HEAD fails, try GET
        try:
            session = get_http_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as response:
                return response.status == 200
        except Exception:
            return False

# Persistent view functions (for buttons that survive bot restarts)
async def save_persistent_view(message_id: int, channel_id: int, view_type: str, view_data: dict, expires_at: Optional[float] = None) -> None:
    """Save a view to the database so it can be restored after bot restart.
    Saves to local SQLite (primary) and Firebase (backup).
    
    Args:
        message_id: Discord message ID
        channel_id: Discord channel ID
        view_type: Type of view ('size_selection', 'sticker_size', 'batch_size')
        view_data: Dictionary containing all data needed to reconstruct the view
        expires_at: Optional expiration timestamp (None = never expires)
    """
    # Save to local SQLite (primary)
    try:
        async with aiosqlite.connect(PERSISTENT_DB_FILE) as db:
            await db.execute('''
                INSERT OR REPLACE INTO persistent_views 
                (message_id, channel_id, view_type, view_data, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                str(message_id),
                str(channel_id),
                view_type,
                json.dumps(view_data),
                expires_at,
                time.time()
            ))
            await db.commit()
    except Exception as e:
        print(f"Error saving persistent view to local DB: {e}")
    
    # Backup to Firebase (async, don't wait for it)
    asyncio.create_task(save_persistent_view_firebase_backup(message_id, channel_id, view_type, view_data, expires_at))

async def save_persistent_view_firebase_backup(message_id: int, channel_id: int, view_type: str, view_data: dict, expires_at: Optional[float] = None) -> None:
    """Backup persistent view to Firebase (writes only)."""
    if not firestore_db:
        return
    
    try:
        # Ensure message_id and channel_id are valid non-empty strings FIRST
        message_id_str = str(message_id).strip()
        channel_id_str = str(channel_id).strip()
        view_type_str = str(view_type).strip()
        
        if not message_id_str or not channel_id_str or not view_type_str:
            print(f"Invalid IDs for Firebase backup: message_id={message_id}, channel_id={channel_id}, view_type={view_type}")
            return
        
        # Clean and serialize view_data for Firestore
        # Firestore doesn't like None values or complex nested structures
        cleaned_view_data = {}
        for key, value in view_data.items():
            if value is not None:
                key_str = str(key).strip()
                if not key_str:
                    continue  # Skip empty keys
                # Convert to JSON string for complex nested structures
                if isinstance(value, (dict, list)):
                    try:
                        serialized = json.dumps(value, ensure_ascii=False)
                        if serialized:  # Only add if not empty
                            cleaned_view_data[key_str] = serialized
                    except (TypeError, ValueError) as e:
                        # If can't serialize, skip it
                        print(f"Could not serialize {key_str} for Firebase: {e}")
                        continue
                elif isinstance(value, (str, int, float, bool)):
                    # Only add non-empty strings
                    if isinstance(value, str) and not value.strip():
                        continue
                    cleaned_view_data[key_str] = value
                else:
                    # Convert other types to string, but skip if empty
                    value_str = str(value).strip()
                    if value_str:
                        cleaned_view_data[key_str] = value_str
        
        doc_ref = firestore_db.collection('persistent_views').document(message_id_str)
        
        # Build document data, only including non-None values
        doc_data = {
            'message_id': message_id_str,
            'channel_id': channel_id_str,
            'view_type': str(view_type).strip(),
            'view_data': cleaned_view_data,
            'created_at': firestore.SERVER_TIMESTAMP,
            'updated_at': firestore.SERVER_TIMESTAMP
        }
        
        # Only add expires_at if it's not None
        if expires_at is not None:
            doc_data['expires_at'] = float(expires_at)
        
        doc_ref.set(doc_data, merge=True)
    except Exception as e:
        print(f"Error backing up persistent view to Firebase: {e}")
        import traceback
        traceback.print_exc()

async def delete_persistent_view(message_id: int) -> None:
    """Remove a view from the database (local and Firebase)."""
    # Delete from local SQLite
    try:
        async with aiosqlite.connect(PERSISTENT_DB_FILE) as db:
            await db.execute('DELETE FROM persistent_views WHERE message_id = ?', (str(message_id),))
            await db.commit()
    except Exception as e:
        print(f"Error deleting persistent view from local DB: {e}")
    
    # Delete from Firebase (async)
    if firestore_db:
        asyncio.create_task(delete_persistent_view_firebase_backup(message_id))

async def delete_persistent_view_firebase_backup(message_id: int) -> None:
    """Delete persistent view from Firebase backup."""
    if not firestore_db:
        return
    
    try:
        doc_ref = firestore_db.collection('persistent_views').document(str(message_id))
        doc_ref.delete()
    except Exception as e:
        print(f"Error deleting persistent view from Firebase: {e}")

async def load_all_persistent_views() -> list:
    """Load all active views from local database (primary).
    If local DB is empty, attempts to restore from Firebase backup.
    
    Returns:
        List of tuples: (message_id, channel_id, view_type, view_data, expires_at)
    """
    views = []
    current_time = time.time()
    
    try:
        async with aiosqlite.connect(PERSISTENT_DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('''
                SELECT message_id, channel_id, view_type, view_data, expires_at
                FROM persistent_views
                WHERE expires_at IS NULL OR expires_at > ?
            ''', (current_time,)) as cursor:
                rows = await cursor.fetchall()
                
                for row in rows:
                    try:
                        view_data = json.loads(row['view_data'])
                        views.append((
                            int(row['message_id']),
                            int(row['channel_id']),
                            row['view_type'],
                            view_data,
                            row['expires_at']
                        ))
                    except Exception as e:
                        print(f"Error parsing view data for {row['message_id']}: {e}")
        
        # Clean up expired views
        async with aiosqlite.connect(PERSISTENT_DB_FILE) as db:
            await db.execute('DELETE FROM persistent_views WHERE expires_at IS NOT NULL AND expires_at <= ?', (current_time,))
            await db.commit()
        
        # If local DB is empty, try to restore from Firebase backup
        if not views and firestore_db:
            print("📦 Local persistent views database is empty, checking Firebase backup...")
            views = await load_persistent_views_from_firebase()
            # Restore to local DB
            if views:
                for message_id, channel_id, view_type, view_data, expires_at in views:
                    await save_persistent_view(message_id, channel_id, view_type, view_data, expires_at)
                print(f"Restored {len(views)} views from Firebase backup to local database")
            
    except Exception as e:
        print(f"Error loading persistent views: {e}")
    
    return views

async def load_persistent_views_from_firebase() -> list:
    """Load all active views from Firebase backup (only if local DB is empty).
    
    Returns:
        List of tuples: (message_id, channel_id, view_type, view_data, expires_at)
    """
    if not firestore_db:
        return []
    
    views = []
    current_time = time.time()
    
    try:
        collection_ref = firestore_db.collection('persistent_views')
        docs = collection_ref.stream()
        
        for doc in docs:
            try:
                data = doc.to_dict()
                if not data:
                    continue
                
                expires_at = data.get('expires_at')
                # Check if expired
                if expires_at and expires_at <= current_time:
                    continue
                
                # Parse view_data (might be JSON strings for complex structures)
                view_data = data.get('view_data', {})
                if isinstance(view_data, dict):
                    # Check if values are JSON strings that need parsing
                    parsed_view_data = {}
                    for key, value in view_data.items():
                        if isinstance(value, str):
                            try:
                                # Try to parse as JSON
                                parsed_view_data[key] = json.loads(value)
                            except (json.JSONDecodeError, TypeError):
                                # Not JSON, use as-is
                                parsed_view_data[key] = value
                        else:
                            parsed_view_data[key] = value
                    view_data = parsed_view_data
                
                views.append((
                    int(data['message_id']),
                    int(data['channel_id']),
                    data['view_type'],
                    view_data,
                    expires_at
                ))
            except Exception as e:
                print(f"Error parsing Firebase view data for {doc.id}: {e}")
                import traceback
                traceback.print_exc()
                
    except Exception as e:
        print(f"Error loading persistent views from Firebase: {e}")
        import traceback
        traceback.print_exc()
    
    return views

async def restore_persistent_views() -> None:
    """Restore all active views and reattach them to their messages."""
    views = await load_all_persistent_views()
    
    if not views:
        print("📋 No persistent views to restore")
        return
    
    restored = 0
    failed = 0
    
    for message_id, channel_id, view_type, view_data, expires_at in views:
        try:
            # Recreate the appropriate view based on type (restored views are persistent, not ephemeral)
            if view_type == 'size_selection':
                view = SizeSelectionView(
                    view_data['emoji_data'],
                    view_data.get('original_size', 128),
                    persistent=True  # Restored views are persistent
                )
            elif view_type == 'sticker_size':
                view = StickerSizeSelectionView(
                    view_data['sticker_data'],
                    view_data.get('original_size', 160),
                    persistent=True  # Restored views are persistent
                )
            elif view_type == 'batch_size':
                view = BatchSizeView(
                    view_data['emojis_data'],
                    persistent=True  # Restored views are persistent
                )
            else:
                print(f"Unknown view type: {view_type} for message {message_id} - removing from database")
                await delete_persistent_view(message_id)
                failed += 1
                continue
            
            # Set message_id for custom_id generation to enable persistent view functionality
            view.message_id = message_id
            view.is_ephemeral = False  # Restored views are not ephemeral
            
            # Configure custom_id for all buttons/selects using message_id for persistent views
            if hasattr(view, '_setup_button_ids'):
                view._setup_button_ids()
            if hasattr(view, '_setup_select_id'):
                view._setup_select_id()
            
            # Register the view with Discord.py - this connects buttons to interactions
            # bot.add_view() registers the view without requiring message fetch
            # to handle interactions for that message_id. Discord will route interactions to us.
            # This works even for DM channels we can't access directly.
            bot.add_view(view, message_id=message_id)
            
            restored += 1
            print(f"Restored {view_type} view for message {message_id} in channel {channel_id}")
            
        except Exception as e:
            print(f"⚠️ Error restoring view for message {message_id}: {e}")
            import traceback
            traceback.print_exc()
            # Don't delete - might be temporary issue, will retry on next restart
            failed += 1
            continue
            
        except discord.errors.NotFound:
            # Message was deleted, remove from database
            print(f"Message {message_id} not found - removing from database")
            await delete_persistent_view(message_id)
            failed += 1
        except discord.errors.Forbidden:
            # No permission to access message, remove from database
            print(f"No permission to access message {message_id} - removing from database")
            await delete_persistent_view(message_id)
            failed += 1
        except Exception as e:
            print(f"⚠️ Error restoring view for message {message_id}: {e}")
            # Don't delete on unknown errors, might be temporary
            failed += 1
    
    print(f"📋 Restored {restored} persistent views ({failed} failed)")

async def check_url_valid(url: str) -> bool:
    """Check if a URL is still valid and accessible.
    Uses shared HTTP session for better performance.
    
    Args:
        url: URL to check
        
    Returns:
        True if URL is accessible (status 200), False otherwise
    """
    try:
        session = get_http_session()
        async with session.head(url, allow_redirects=True) as response:
            return response.status == 200
    except asyncio.TimeoutError:
        return False
    except Exception:
        # Silently fail - URL is invalid
        return False

async def check_urls_valid_batch(urls: list[str], max_concurrent: int = 10) -> Dict[str, bool]:
    """Check multiple URLs concurrently with rate limiting.
    
    Args:
        urls: List of URLs to check
        max_concurrent: Maximum number of concurrent checks
        
    Returns:
        Dictionary mapping URL -> bool (valid/invalid)
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    results = {}
    
    async def check_with_semaphore(url: str):
        async with semaphore:
            is_valid = await check_url_valid(url)
            results[url] = is_valid
            return url, is_valid
    
    # Check all URLs concurrently with rate limiting
    tasks = [check_with_semaphore(url) for url in urls]
    await asyncio.gather(*tasks, return_exceptions=True)
    
    return results

async def download_sticker(sticker_url: str) -> Optional[bytes]:
    """Download sticker from Discord CDN.
    Uses shared HTTP session for better performance.
    
    Args:
        sticker_url: URL of the sticker to download
        
    Returns:
        Bytes of the downloaded sticker, or None if failed
    """
    try:
        session = get_http_session()
        async with session.get(sticker_url) as response:
            if response.status == 200:
                return await response.read()
    except Exception as e:
        print(f"Error downloading sticker: {e}")
    return None

def _calculate_gif_content_bbox(img: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """Calculate combined content bounds across all frames (non-transparent pixels)."""
    try:
        total_frames = getattr(img, 'n_frames', 1)
    except Exception:
        total_frames = 1
    
    bbox = None
    # Generous padding (~12% of longest edge) but not less than 24px
    padding = max(24, int(max(img.size) * 0.12))
    
    for frame_num in range(max(1, total_frames)):
        try:
            img.seek(frame_num)
            frame = img.convert('RGBA')
        except EOFError:
            break
        except Exception:
            continue
        
        alpha = frame.split()[-1]
        frame_bbox = alpha.getbbox()
        if frame_bbox:
            if bbox is None:
                bbox = frame_bbox
            else:
                bbox = (
                    min(bbox[0], frame_bbox[0]),
                    min(bbox[1], frame_bbox[1]),
                    max(bbox[2], frame_bbox[2]),
                    max(bbox[3], frame_bbox[3]),
                )
    
    # Reset to first frame for future callers
    try:
        img.seek(0)
    except Exception:
        pass
    
    if not bbox:
        return None
    
    left = max(0, bbox[0] - padding)
    top = max(0, bbox[1] - padding)
    right = min(img.size[0], bbox[2] + padding)
    bottom = min(img.size[1], bbox[3] + padding)
    
    return (left, top, right, bottom)


async def _process_gif_bytes(gif_data: bytes, target_size: int) -> Optional[bytes]:
    """Process GIF bytes and resize to target size.
    Helper function to process already-converted GIF data.
    
    Args:
        gif_data: GIF image bytes
        target_size: Target size in pixels
        
    Returns:
        Processed GIF bytes, or None if failed
    """
    try:
        img = Image.open(io.BytesIO(gif_data))
        is_animated = getattr(img, 'is_animated', False)
        content_bbox = _calculate_gif_content_bbox(img)
        if content_bbox:
            bbox_w = content_bbox[2] - content_bbox[0]
            bbox_h = content_bbox[3] - content_bbox[1]
            coverage = min(bbox_w / max(1, img.size[0]), bbox_h / max(1, img.size[1]))
            # Skip cropping if bounding box already fills most of the frame
            if coverage >= 0.95:
                content_bbox = None
        output = io.BytesIO()
        
        if is_animated:
            frames = []
            durations = []
            
            try:
                total_frames = getattr(img, 'n_frames', 1)
                
                for frame_num in range(total_frames):
                    img.seek(frame_num)
                    img.load()
                    frame = img.copy()
                    
                    if frame.mode != 'RGBA':
                        frame = frame.convert('RGBA')
                    
                    if content_bbox:
                        frame = frame.crop(content_bbox)
                    
                    scale_w = target_size / frame.size[0]
                    scale_h = target_size / frame.size[1]
                    scale = min(scale_w, scale_h)
                    
                    new_width = int(frame.size[0] * scale)
                    new_height = int(frame.size[1] * scale)
                    frame = frame.resize((new_width, new_height), Image.Resampling.LANCZOS)
                    
                    # Create transparent background frame
                    new_frame = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
                    x_offset = (target_size - frame.size[0]) // 2
                    y_offset = (target_size - frame.size[1]) // 2
                    # Paste with alpha channel to preserve transparency
                    new_frame.paste(frame, (x_offset, y_offset), frame)
                    
                    frames.append(new_frame)
                    
                    # Get duration
                    duration_info = img.info.get('duration', 100)
                    if isinstance(duration_info, (list, tuple)):
                        duration = duration_info[frame_num] if frame_num < len(duration_info) else 100
                    else:
                        duration = duration_info if isinstance(duration_info, (int, float)) else 100
                    durations.append(int(duration))
                        
            except Exception as frame_error:
                print(f"Error processing GIF frame: {frame_error}")
                if not frames:
                    raise frame_error
            
            if frames:
                default_duration = durations[0] if durations else 100
                # Save with disposal=3 (restore to previous) to preserve transparency
                # This prevents black backgrounds that occur with disposal=2 (restore to background)
                frames[0].save(
                    output,
                    format='GIF',
                    save_all=True,
                    append_images=frames[1:] if len(frames) > 1 else [],
                    duration=durations if len(durations) > 1 else default_duration,
                    loop=0,
                    optimize=False,
                    disposal=3  # Restore to previous (preserves transparency, avoids black backgrounds)
                )
            else:
                raise ValueError("No frames extracted from GIF")
        else:
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            
            if content_bbox:
                img = img.crop(content_bbox)
            
            scale_w = target_size / img.size[0]
            scale_h = target_size / img.size[1]
            scale = min(scale_w, scale_h)
            
            new_width = int(img.size[0] * scale)
            new_height = int(img.size[1] * scale)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            new_img = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
            x_offset = (target_size - img.size[0]) // 2
            y_offset = (target_size - img.size[1]) // 2
            new_img.paste(img, (x_offset, y_offset), img)
            new_img.save(output, format='GIF', save_all=True)
        
        output.seek(0)
        return output.read()
    except Exception as e:
        print(f"Error processing GIF bytes: {e}")
        import traceback
        traceback.print_exc()
    return None

async def process_and_resize_sticker(sticker_data: bytes, target_size: int, is_lottie: bool = False) -> Optional[bytes]:
    """Process sticker and resize to target size.
    Converts APNG to GIF format for Discord embed compatibility.
    Note: Lottie stickers are not processed - they return None.
    
    Args:
        sticker_data: Raw sticker bytes (image for APNG)
        target_size: Target size in pixels
        is_lottie: Whether this is a Lottie sticker (not processed)
        
    Returns:
        Processed sticker bytes (GIF format), or None if failed or Lottie
    """
    try:
        # Lottie stickers are not processed - return None
        if is_lottie:
                return None
        
        # Open image from bytes (for APNG)
        img = Image.open(io.BytesIO(sticker_data))
        
        # Check if animated
        is_animated = getattr(img, 'is_animated', False)
        
        output = io.BytesIO()
        
        if is_animated:
            # For animated images (APNG), extract and process each frame individually
            frames = []
            durations = []
            
            try:
                # Get total number of frames
                total_frames = getattr(img, 'n_frames', 1)
                
                for frame_num in range(total_frames):
                    # Seek to specific frame
                    img.seek(frame_num)
                    
                    # Load the frame data first
                    img.load()
                    
                    # Copy this frame (now it's safe to copy)
                    frame = img.copy()
                    
                    # Convert to RGBA if needed (for transparency)
                    if frame.mode != 'RGBA':
                        frame = frame.convert('RGBA')
                    
                    # Calculate scale factor to fit within target size (maintain aspect ratio, no cropping)
                    # Scale to fit within the canvas without cropping
                    scale_w = target_size / frame.size[0]
                    scale_h = target_size / frame.size[1]
                    scale = min(scale_w, scale_h)  # Use smaller scale to fit within canvas without cropping
                    
                    # Resize frame to fit target size
                    new_width = int(frame.size[0] * scale)
                    new_height = int(frame.size[1] * scale)
                    frame = frame.resize((new_width, new_height), Image.Resampling.LANCZOS)
                    
                    # Create a new image with exact target size and transparent background
                    new_frame = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
                    
                    # Center the resized frame in the new canvas (no cropping, maintains aspect ratio)
                    x_offset = (target_size - frame.size[0]) // 2
                    y_offset = (target_size - frame.size[1]) // 2
                    new_frame.paste(frame, (x_offset, y_offset), frame)
                    
                    frames.append(new_frame)
                    
                    # Get frame duration (handle both single value and list)
                    duration_info = img.info.get('duration', 100)
                    if isinstance(duration_info, (list, tuple)):
                        duration = duration_info[frame_num] if frame_num < len(duration_info) else 100
                    else:
                        duration = duration_info if isinstance(duration_info, (int, float)) else 100
                    durations.append(int(duration))
                        
            except Exception as frame_error:
                print(f"Error processing frame: {frame_error}")
                import traceback
                traceback.print_exc()
                # If we got at least one frame, continue
                if not frames:
                    raise frame_error
            
            if frames:
                # Save all frames as animated GIF
                # Use disposal=3 (restore to previous) to preserve transparency and avoid black backgrounds
                default_duration = durations[0] if durations else 100
                frames[0].save(
                    output,
                    format='GIF',
                    save_all=True,
                    append_images=frames[1:] if len(frames) > 1 else [],
                    duration=durations if len(durations) > 1 else default_duration,
                    loop=0,
                    optimize=False,
                    disposal=3  # Restore to previous (preserves transparency, avoids black backgrounds)
                )
            else:
                raise ValueError("No frames extracted from animated image")
        else:
            # Static image processing
            # Convert to RGBA if needed
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            
            # Calculate scale factor to fit within target size (maintain aspect ratio, no cropping)
            # Scale to fit within the canvas without cropping
            scale_w = target_size / img.size[0]
            scale_h = target_size / img.size[1]
            scale = min(scale_w, scale_h)  # Use smaller scale to fit within canvas without cropping
            
            # Resize image to fit target size
            new_width = int(img.size[0] * scale)
            new_height = int(img.size[1] * scale)
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # Create new image with exact target size and transparent background
            new_img = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
            
            # Center the resized image (no cropping, maintains aspect ratio)
            x_offset = (target_size - img.size[0]) // 2
            y_offset = (target_size - img.size[1]) // 2
            new_img.paste(img, (x_offset, y_offset), img)
            
            # Save as GIF
            new_img.save(output, format='GIF', save_all=True)
        
        output.seek(0)
        return output.read()
    except Exception as e:
        print(f"Error processing sticker: {e}")
        import traceback
        traceback.print_exc()
    return None

async def upload_sticker_to_backend(sticker_id: str, size: int, sticker_bytes: bytes) -> Optional[tuple]:
    """Upload processed sticker to backend Discord channel and get CDN URL and message ID.
    
    Args:
        sticker_id: Sticker ID
        size: Size of the sticker
        sticker_bytes: Processed sticker bytes to upload
        
    Returns:
        Tuple of (CDN URL, message_id) or None if failed
    """
    if not BACKEND_GUILD_ID or not BACKEND_CHANNEL_ID:
        return None
    
    try:
        guild = bot.get_guild(BACKEND_GUILD_ID)
        if not guild:
            print(f"Backend guild {BACKEND_GUILD_ID} not found")
            return None
        
        channel = guild.get_channel(BACKEND_CHANNEL_ID)
        if not channel:
            print(f"Backend channel {BACKEND_CHANNEL_ID} not found")
            return None
        
        # Create file object
        filename = f"{sticker_id}_{size}px.gif"
        file = discord.File(io.BytesIO(sticker_bytes), filename=filename)
        
        # Upload to channel
        message = await channel.send(file=file)
        
        # Get CDN URL from attachment
        if message.attachments:
            cdn_url = message.attachments[0].url
            return (cdn_url, message.id)
        
    except Exception as e:
        print(f"Error uploading sticker to backend: {e}")
        import traceback
        traceback.print_exc()
    return None

async def get_backend_sticker_urls(sticker_id: str, original_sticker_url: str, is_lottie: bool = False) -> Optional[tuple]:
    """Get or create backend URLs for animated sticker in all sizes.
    
    Stickers are permanently stored in the backend channel. Cached URLs are verified
    before reuse - if they expire, new uploads are created.
    
    Args:
        sticker_id: Sticker ID
        original_sticker_url: Original Discord sticker URL
        is_lottie: Whether this is a Lottie sticker
        
    Returns:
        Tuple of (urls_dict, message_ids_list) where:
        - urls_dict: Dictionary mapping size -> CDN URL
        - message_ids_list: List of message IDs (stored permanently, never deleted)
        Or None if failed
    """
    # Check database cache first
    cached_data = await get_cached_sticker_urls(sticker_id)
    cached_urls = {}
    cached_message_ids = []
    cached_message_ids_map = {}
    
    if cached_data:
        cached_urls = cached_data.get('urls', {})
        cached_message_ids = cached_data.get('message_ids', [])
        cached_message_ids_map = cached_data.get('message_ids_map', {})
        
        # Verify all cached URLs are still valid (optimized concurrent checks)
        print(f"Checking cached URLs for sticker {sticker_id}...")
        valid_urls = {}
        invalid_sizes = []
        
        # Prepare URL list for batch validation
        url_list = list(cached_urls.values())
        size_to_url = {size: url for size, url in cached_urls.items()}
        url_to_size = {url: size for size, url in cached_urls.items()}
        
        # Batch validate all URLs concurrently with rate limiting
        validation_results = await check_urls_valid_batch(url_list, max_concurrent=10)
        
        # Process results
        for url, is_valid in validation_results.items():
            size = url_to_size[url]
            if is_valid:
                valid_urls[size] = url
                print(f"  ✓ Cached URL for {size}px is still valid")
            else:
                invalid_sizes.append(size)
                print(f"  ✗ Cached URL for {size}px has expired, will re-upload")
        
        # Update database to only include valid URLs (remove expired ones)
        if invalid_sizes:
            await update_cached_urls(sticker_id, valid_urls)
            print(f"Updated cache: removed {len(invalid_sizes)} expired URLs for sticker {sticker_id}")
        
        # If all URLs are valid, return cached data
        if len(valid_urls) == len(cached_urls) and len(valid_urls) == len(STICKER_SIZES):
            print(f"All cached URLs for sticker {sticker_id} are still valid, reusing cache")
            return valid_urls, cached_message_ids
        
        # Some URLs expired, we'll need to re-upload those sizes
        if invalid_sizes:
            print(f"Re-uploading expired sizes for sticker {sticker_id}: {invalid_sizes}")
    
    # Start with valid cached URLs, or empty dict if no cache
    urls = valid_urls if 'valid_urls' in locals() else {}
    message_ids = cached_message_ids if 'cached_message_ids' in locals() else []
    
    # Determine which sizes need to be uploaded
    if 'invalid_sizes' in locals():
        # Some cached URLs expired, need to re-upload those
        sizes_to_upload = invalid_sizes.copy()
        # Also check for any sizes that weren't in cache at all
        for size in STICKER_SIZES:
            if size not in urls and size not in sizes_to_upload:
                sizes_to_upload.append(size)
    else:
        # No cache, upload all sizes
        sizes_to_upload = STICKER_SIZES.copy()
    
    # If all sizes are cached and valid, return early
    if not sizes_to_upload and len(urls) == len(STICKER_SIZES):
        return (urls, message_ids) if urls else None
    
    # Download original sticker (only needed for sizes we're uploading)
    sticker_data = None
    if sizes_to_upload:
        print(f"Downloading sticker {sticker_id} for processing...")
        sticker_data = await download_sticker(original_sticker_url)
        if not sticker_data:
            # If we have some valid cached URLs, return those instead of failing completely
            if urls:
                print(f"Failed to download original sticker, but returning {len(urls)} cached URLs")
                return (urls, message_ids)
            # No cached URLs and download failed - log error and return None
            print(f"ERROR: Failed to download sticker {sticker_id} from {original_sticker_url} and no cached URLs available")
            return None
    
    # Process and upload missing/invalid sizes
    
    # Optimization: Process the largest size first to get a master GIF
    # Then resize that master GIF for all other sizes instead of re-converting/re-extracting
    # This significantly speeds up processing (converting once instead of multiple times)
    master_gif_bytes = None
    
    # Sort sizes to upload so we do largest first (descending order)
    # This ensures we get the highest quality source for downscaling
    sorted_sizes_to_upload = sorted(sizes_to_upload, reverse=True)
    largest_size_to_upload = sorted_sizes_to_upload[0]
    
    # If we already have a valid cached URL for a large size, we can use that as master
    # Find the largest cached size to use as master if available
    if urls:
        cached_sizes = sorted(urls.keys(), reverse=True)
        largest_cached = cached_sizes[0] if cached_sizes else None
        
        # Try to download from cached URL to use as master
        if largest_cached and largest_cached > largest_size_to_upload:
            print(f"Using cached {largest_cached}px as master for resizing...")
            master_data = await download_sticker(urls[largest_cached])
            if master_data:
                master_gif_bytes = master_data
                print(f"Successfully downloaded master from cached {largest_cached}px URL")
    
    # If we don't have a master yet, process the largest size from original sticker
    if not master_gif_bytes:
        print(f"Processing master sticker {sticker_id} at size {largest_size_to_upload}px...")
        processed_data = await process_and_resize_sticker(sticker_data, largest_size_to_upload, is_lottie=is_lottie)
        if processed_data:
            master_gif_bytes = processed_data
        else:
            print(f"Failed to process master size {largest_size_to_upload}px")
            # If we can't process and have no cached URLs, return None
            return (urls, message_ids) if urls else None
    
    # Upload the largest size if it's in sizes_to_upload and we have master_gif_bytes
    master_msg_id = None
    if master_gif_bytes and largest_size_to_upload in sizes_to_upload:
        result = await upload_sticker_to_backend(sticker_id, largest_size_to_upload, master_gif_bytes)
        if result:
            cdn_url, msg_id = result
            urls[largest_size_to_upload] = cdn_url
            master_msg_id = str(msg_id)
            message_ids.append(msg_id)
            print(f"Uploaded {sticker_id} at {largest_size_to_upload}px: {cdn_url}")
        else:
            print(f"Failed to upload {sticker_id} at {largest_size_to_upload}px")
    
    # Process all other sizes using the master GIF (fast resize)
    async def process_and_upload_size(size):
        if size not in sizes_to_upload:
            return None  # Already cached and valid, skip
            
        # Resize from master GIF bytes instead of original sticker data
        # This skips APNG frame extraction logic
        resized_data = await _process_gif_bytes(master_gif_bytes, size)
        
        if resized_data:
            result = await upload_sticker_to_backend(sticker_id, size, resized_data)
            if result:
                cdn_url, msg_id = result
                print(f"Uploaded {sticker_id} at {size}px: {cdn_url}")
                return size, cdn_url, msg_id
            else:
                print(f"Failed to upload {sticker_id} at {size}px")
        else:
            print(f"Failed to process {sticker_id} at {size}px")
        return None

    # Create tasks for sizes that need uploading (excluding the master size already uploaded)
    upload_tasks = []
    for size in sorted_sizes_to_upload:
        if size != largest_size_to_upload or largest_size_to_upload not in urls:
            upload_tasks.append(process_and_upload_size(size))
    
    # Build size -> message_id mapping for newly uploaded stickers
    message_ids_map = {}
    # Add master size message_id if it was uploaded
    if 'master_msg_id' in locals() and master_msg_id and largest_size_to_upload in urls:
        message_ids_map[largest_size_to_upload] = master_msg_id
    
    if upload_tasks:
        results = await asyncio.gather(*upload_tasks)
        
        # Collect results and build message_ids_map
        for res in results:
            if res:
                size, cdn_url, msg_id = res
                urls[size] = cdn_url
                message_ids_map[size] = str(msg_id)
                message_ids.append(msg_id)
    
    # Add message_id for master size if it was uploaded
    if largest_size_to_upload in sizes_to_upload and largest_size_to_upload in urls:
        # Find the message_id we got from uploading the master size
        # We already added it to message_ids list, but we need to map it
        # Since we uploaded it, the message_id should be the last one added
        # Actually, let's check if we have it from the upload
        pass  # message_ids_map will be built from upload results above
    
    # Update database cache with all URLs (including newly uploaded ones)
    # Messages are stored permanently, never deleted
    if urls:
        # Build complete message_ids_map: new uploads + preserved cached ones
        complete_message_ids_map = message_ids_map.copy() if 'message_ids_map' in locals() else {}
        # Add message_ids from cached_data if available (preserve existing message_ids for sizes we're keeping)
        if 'cached_message_ids_map' in locals() and cached_message_ids_map:
            for size in urls.keys():
                if size in cached_message_ids_map and size not in complete_message_ids_map:
                    complete_message_ids_map[size] = cached_message_ids_map[size]
        await cache_sticker_urls(sticker_id, urls, complete_message_ids_map if complete_message_ids_map else None)
        print(f"Cached {len(urls)} URLs for sticker {sticker_id} in database (stored permanently)")
    
    return (urls, message_ids) if urls else None

# Health check server for Railway/other hosting services

def format_uptime(seconds):
    """Format seconds into human-readable uptime string"""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    
    return " ".join(parts) if parts else "0s"

def get_uptime():
    """Get current uptime in seconds"""
    global start_time
    if start_time is None:
        return 0
    return time.time() - start_time

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            bot_ready = False
            if bot_instance:
                try:
                    bot_ready = bot_instance.is_ready()
                except:
                    pass
            
            uptime_seconds = get_uptime()
            response = {
                'status': 'ok',
                'bot_ready': bot_ready,
                'uptime_seconds': uptime_seconds,
                'uptime_formatted': format_uptime(uptime_seconds),
                'timestamp': time.time()
            }
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Suppress health check logs
        pass

def run_health_server(port):
    if port <= 0:
        return
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f'🏥 Health check server running on port {port} (http://0.0.0.0:{port}/health)')
    server.serve_forever()

@bot.event
async def on_disconnect():
    """Cleanup when bot disconnects."""
    await close_http_session()
    print("🔌 Bot disconnected, cleaned up resources")

@bot.event
async def on_ready():
    global start_time
    if start_time is None:
        start_time = time.time()
    print(f'🚀 {bot.user} is online!')
    print(f'📊 Serving {len(bot.guilds)} servers')
    if start_time:
        print(f'⏰ Bot started at {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))}')
    else:
        uptime = format_uptime(get_uptime())
        print(f'🔄 Bot reconnected! Uptime: {uptime}')
    
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, 
        name="emoji & sticker IDs → CDN links"
    ))

@bot.event
async def setup_hook():
    # Initialize local SQLite database (primary)
    await init_local_database()
    
    # Initialize Firebase Firestore (backup only)
    init_firestore()
    
    # Restore persistent views from database
    await restore_persistent_views()
    
    try:
        # ONLY sync to the specific test guild if configured
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj) # Optional: copy global to test guild
            synced_guild = await bot.tree.sync(guild=guild_obj)
            print(f"Synced {len(synced_guild)} application commands to guild {GUILD_ID} (instant)")
        
        # REMOVED: await bot.tree.sync() (Global Sync)
        print("ℹ️ Global sync is disabled. Use /status to sync manually.")
            
    except Exception as e:
        print(f"Failed to sync application commands: {e}")
    
    # Start uptime logging task
    log_uptime.start()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global error handler for app commands."""
    if isinstance(error, app_commands.CommandInvokeError):
        # Check if the underlying error is a rate limit
        original_error = error.original
        if isinstance(original_error, discord.errors.HTTPException) and original_error.status == 429:
            # Try to extract retry_after from response headers or use default
            retry_after = 5.0
            try:
                # Check if retry_after is in the response
                if hasattr(original_error, 'response') and original_error.response:
                    retry_after_header = original_error.response.headers.get('Retry-After')
                    if retry_after_header:
                        retry_after = float(retry_after_header)
                elif hasattr(original_error, 'retry_after'):
                    retry_after = float(original_error.retry_after)
            except (ValueError, AttributeError, TypeError):
                retry_after = 5.0  # Default to 5 seconds
            
            try:
                error_msg = f"⚠️ Rate limited by Discord/Cloudflare. Please try again in {retry_after:.0f} seconds."
                if interaction.response.is_done():
                    await interaction.followup.send(error_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(error_msg, ephemeral=True)
            except Exception as e:
                # If we can't send anything, just log it
                print(f"Rate limited on command {interaction.command.name if interaction.command else 'unknown'}. Retry after: {retry_after}s. Error: {e}")
            return
    
    # For other errors, log and optionally send a message
    print(f"Error in command {interaction.command.name if interaction.command else 'unknown'}: {error}")
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ An error occurred while processing your command. Please try again later.",
                ephemeral=True
            )
    except:
        pass

@tasks.loop(hours=1.0)
async def log_uptime():
    """Log bot uptime every hour"""
    if start_time is not None:
        uptime = format_uptime(get_uptime())
        guild_count = len(bot.guilds) if bot.is_ready() else 0
        print(f'⏱️  Bot uptime: {uptime} | Serving {guild_count} servers')

@log_uptime.before_loop
async def before_log_uptime():
    """Wait until bot is ready before starting uptime logging"""
    await bot.wait_until_ready()
    # Log immediately on first run
    if start_time is not None:
        uptime = format_uptime(get_uptime())
        guild_count = len(bot.guilds) if bot.is_ready() else 0
        print(f'⏱️  Bot uptime: {uptime} | Serving {guild_count} servers')

# Context Menu Commands
@bot.tree.context_menu(name="Extract Emoji & Sticker Links")
async def extract_emoji_links(interaction: discord.Interaction, message: discord.Message):
    # Defer response to prevent timeout during async operations
    await interaction.response.defer(ephemeral=True)
    
    # Check for emojis in message content
    emoji_matches = re.findall(r'<(a)?:(\w+):(\d{15,20})>', message.content)

    # Check for stickers in message
    sticker_results = []
    if message.stickers:
        for sticker_item in message.stickers[:10]:
            sticker_id = str(sticker_item.id)
            
            # Check if sticker is animated using 'format' attribute (not 'format_type')
            is_animated = False
            is_gif_format = False
            is_lottie_format = False
            sticker_format = None
            sticker_url_property = None
            try:
                # StickerItem already has 'format' attribute, no need to fetch
                sticker_format = sticker_item.format
                
                if hasattr(sticker_item, 'url'):
                    sticker_url_property = sticker_item.url
                
                # Check if format indicates animated sticker (APNG, Lottie, or GIF)
                if isinstance(sticker_format, discord.StickerFormatType):
                    is_animated = sticker_format in (discord.StickerFormatType.apng, discord.StickerFormatType.lottie, discord.StickerFormatType.gif)
                    is_gif_format = sticker_format == discord.StickerFormatType.gif
                    is_lottie_format = sticker_format == discord.StickerFormatType.lottie
                elif hasattr(sticker_format, 'value'):
                    # Enum with value attribute (2=APNG, 3=Lottie, 4=GIF)
                    format_value = sticker_format.value if hasattr(sticker_format, 'value') else sticker_format
                    is_animated = format_value in (2, 3, 4)
                    is_gif_format = format_value == 4  # 4 = GIF
                    is_lottie_format = format_value == 3  # 3 = Lottie
                else:
                    # Fallback: check if format is integer (2=APNG, 3=Lottie, 4=GIF)
                    format_value = sticker_format if isinstance(sticker_format, int) else getattr(sticker_format, 'value', None)
                    if format_value == 4:
                        is_gif_format = True
                        is_animated = True
                    elif format_value == 3:
                        is_lottie_format = True
                        is_animated = True
                
                print(f"\n{'='*60}")
                print(f"Processing Sticker ID: {sticker_id}")
                print(f"{'='*60}")
                print(f"StickerItem properties:")
                print(f"  - id: {sticker_item.id}")
                print(f"  - name: {getattr(sticker_item, 'name', 'N/A')}")
                print(f"  - format: {sticker_format}")
                print(f"  - url property: {sticker_url_property}")
                print(f"  - All attributes: {[attr for attr in dir(sticker_item) if not attr.startswith('_')]}")
                
                # Try to fetch full sticker to see what it provides
                try:
                    full_sticker = await sticker_item.fetch()
                    print(f"\nFull Sticker object properties:")
                    print(f"  - id: {full_sticker.id}")
                    print(f"  - name: {getattr(full_sticker, 'name', 'N/A')}")
                    print(f"  - format: {getattr(full_sticker, 'format', 'N/A')}")
                    if hasattr(full_sticker, 'url'):
                        print(f"  - url property: {full_sticker.url}")
                    print(f"  - All attributes: {[attr for attr in dir(full_sticker) if not attr.startswith('_')]}")
                except Exception as fetch_error:
                    print(f"  - Could not fetch full sticker: {fetch_error}")
                
                print(f"\nIs Animated: {is_animated}")
                if is_gif_format:
                    print(f"Format: GIF (Discord native)")
                elif is_lottie_format:
                    print(f"Format: Lottie (JSON animation)")
                else:
                    print(f"Format: APNG (will be converted to GIF)")
            except Exception as e:
                print(f"  - ERROR checking sticker format: {e}")
                import traceback
                traceback.print_exc()
                pass
            
            # For GIF format stickers, use direct Discord CDN URL (Discord supports GIF embeds)
            if is_gif_format and sticker_url_property:
                # Use direct .gif URL with size parameter - Discord supports GIF embeds directly
                base_gif_url = sticker_url_property
                # Remove any existing size parameter
                base_gif_url = re.sub(r'[?&]size=\d+', '', base_gif_url)
                # Add size parameter
                if '?' in base_gif_url:
                    url = f'{base_gif_url}&size={STICKER_DEFAULT_SIZE}'
                else:
                    url = f'{base_gif_url}?size={STICKER_DEFAULT_SIZE}'
                print(f"Using direct GIF URL with size parameter: {url}")
                sticker_results.append({
                    'url': url,
                    'id': sticker_id,
                    'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                    'type': 'sticker_url',
                    'animated': is_animated,
                    'is_gif': True  # Flag to indicate this is a direct GIF URL
                })
                continue
            
            # Handle Lottie stickers separately - provide JSON link only, no processing
            if is_lottie_format:
                # Lottie stickers are not supported - provide JSON link only
                json_url = f'https://cdn.discordapp.com/stickers/{sticker_id}.json'
                sticker_results.append({
                    'url': json_url,
                    'id': sticker_id,
                    'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                    'type': 'sticker_url',
                    'animated': is_animated,
                    'is_lottie': True  # Flag to show unsupported message in embed
                })
                continue
            
            # For APNG animated stickers, use backend server if configured
            # APNG can be converted to GIF for Discord embed compatibility
            if is_animated and not is_gif_format and BACKEND_GUILD_ID and BACKEND_CHANNEL_ID:
                # Get backend URLs (processes and uploads if not cached)
                # Download highest quality version (320px or original)
                if sticker_url_property:
                    # Normalize URL first (convert non-animated PNG to media.discordapp.net)
                    normalized_url = normalize_sticker_url(sticker_url_property, is_animated=is_animated)
                    # Remove any existing size parameter and use highest quality
                    high_quality_url = re.sub(r'[?&]size=\d+', '', normalized_url)
                    if '?' in high_quality_url:
                        original_url = f'{high_quality_url}&size=320'
                    else:
                        original_url = f'{high_quality_url}?size=320'
                else:
                    # Default URL based on format
                    if is_lottie_format:
                        original_url = f'https://cdn.discordapp.com/stickers/{sticker_id}.json?size=320'
                    else:
                        original_url = f'https://media.discordapp.net/stickers/{sticker_id}.png?size=320'
                result = await get_backend_sticker_urls(sticker_id, original_url, is_lottie=is_lottie_format)
                
                if result:
                    backend_urls, message_ids = result
                    if STICKER_DEFAULT_SIZE in backend_urls:
                        # Use backend URL for the default size
                        url = backend_urls[STICKER_DEFAULT_SIZE]
                        print(f"Using backend server URL: {url}")
                        # Store all backend URLs and message IDs in sticker_data
                        sticker_results.append({
                            'url': url,
                            'id': sticker_id,
                            'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                            'type': 'sticker_url',
                            'animated': is_animated,
                            'backend_urls': backend_urls,  # Store all sizes for button switching
                            'uploaded_message_ids': message_ids,  # Store message IDs for deletion
                            'uses_backend': True,  # Flag to indicate using custom server
                            'is_lottie': is_lottie_format  # Store format type for embed messages
                        })
                        continue
                
                print(f"Backend processing failed, falling back to Discord CDN")
            
            # Use Discord's own URL format (cdn.discordapp.com) and add size parameter
            if sticker_url_property:
                # Discord provides: https://cdn.discordapp.com/stickers/{id}.png
                # For non-animated PNG stickers, convert to media.discordapp.net (size parameter works)
                # For animated stickers, cdn.discordapp.com works fine
                base_url = normalize_sticker_url(sticker_url_property, is_animated=is_animated)
                # Check if URL already has parameters
                if '?' in base_url:
                    # Add size parameter to existing query string
                    url = f'{base_url}&size={STICKER_DEFAULT_SIZE}'
                else:
                    # Add size parameter as first query parameter
                    url = f'{base_url}?size={STICKER_DEFAULT_SIZE}'
                print(f"Using Discord's sticker.url (normalized) with size parameter: {url}")
            elif is_animated:
                # Fallback: Use Discord's CDN format with size (works for animated!)
                url = f'https://media.discordapp.net/stickers/{sticker_id}.png?size={STICKER_DEFAULT_SIZE}'
                print(f"Fallback: Using cdn.discordapp.com format: {url}")
            else:
                # For static stickers, use media.discordapp.net with .webp (size parameter works)
                url = f'https://media.discordapp.net/stickers/{sticker_id}.webp?size={STICKER_DEFAULT_SIZE}&quality=lossless'
                print(f"Static sticker: Using media.discordapp.net format: {url}")
            
            print(f"\nFinal URL chosen: {url}")
            print(f"{'='*60}\n")
            sticker_results.append({
                'url': url,
                'id': sticker_id,
                'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                'type': 'sticker_url',
                'animated': is_animated
            })
    
    # Combine emoji and sticker results
    results = []
    
    # Add emojis
    for match in emoji_matches[:10]:
        animated = bool(match[0])
        name = match[1]
        emoji_id = match[2]
        ext = 'gif' if animated else 'webp'
        url = f'https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=48&quality=lossless'
        results.append({'url': url, 'animated': animated, 'id': emoji_id, 'name': name, 'type': 'discord_emoji'})

    # Add stickers
    results.extend(sticker_results)
    
    if not results:
        embed = discord.Embed(
            title="No items found",
            description="This message does not contain any custom emojis or stickers.",
            color=0x2F3136
        )
        embed.add_field(
            name="How to use",
            value=(
                "**Emojis:** Right-click a custom emoji in a message\n"
                "**Stickers:** Right-click a sticker in a message"
            ),
            inline=False
        )
        embed.add_field(
            name="Supported formats",
            value=(
                "**Emojis:** `<:name:123456789>` or `<a:name:123456789>`\n"
                "**Stickers:** Any sticker from Discord's sticker picker"
            ),
            inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Handle single result
    if len(results) == 1:
        parsed = results[0]
        expires_at = int(time.time()) + 300
        # Use appropriate default size based on type
        default_size = STICKER_DEFAULT_SIZE if parsed['type'] in ('sticker_url', 'sticker_id') else 48
        embed = build_single_embed(parsed, default_size, expires_at)
        
        # Use different views for stickers vs emojis (ephemeral messages)
        # Lottie stickers don't get size buttons - only JSON link is provided
        view = None
        if parsed['type'] in ('sticker_url', 'sticker_id') and not parsed.get('is_lottie'):
            view = StickerSizeSelectionView(parsed, default_size, expires_at, is_ephemeral=True)
            # Set button styles
            for item in view.children:
                if isinstance(item, discord.ui.Button):
                    size_str = f"{default_size}px"
                    if size_str in item.label:
                        item.style = discord.ButtonStyle.primary
                    else:
                        item.style = discord.ButtonStyle.secondary
        elif parsed['type'] not in ('sticker_url', 'sticker_id'):
            view = SizeSelectionView(parsed, default_size, expires_at, is_ephemeral=True)
            # Set button styles
            for item in view.children:
                if isinstance(item, discord.ui.Button):
                    size_str = f"{default_size}px"
                    if size_str in item.label:
                        item.style = discord.ButtonStyle.primary
                    else:
                        item.style = discord.ButtonStyle.secondary
        
        if view:
            sent = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            view.message = sent
            await view.on_message_set(sent, is_ephemeral=True)
        else:
            sent = await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Handle multiple results (ephemeral)
    expires_at = int(time.time()) + 300
    # Use emoji default for batch (48px) - could be improved to detect mixed types
    embed = build_batch_embed(results, 48, expires_at)
    view = BatchSizeView(results, expires_at, is_ephemeral=True)
    sent = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    view.message = sent
    await view.on_message_set(sent, is_ephemeral=True)
@bot.tree.context_menu(name="DM Emoji & Sticker Links")
async def dm_emoji_links(interaction: discord.Interaction, message: discord.Message):
    # Defer response immediately to prevent timeout during async operations
    await interaction.response.defer(ephemeral=True)
    
    # Check for emojis in message content
    emoji_matches = re.findall(r'<(a)?:(\w+):(\d{15,20})>', message.content)

    # Check for stickers in message
    sticker_results = []
    if message.stickers:
        for sticker_item in message.stickers[:10]:
            sticker_id = str(sticker_item.id)
            
            # Check if sticker is animated using 'format' attribute (not 'format_type')
            is_animated = False
            is_gif_format = False
            is_lottie_format = False
            sticker_format = None
            sticker_url_property = None
            try:
                # StickerItem already has 'format' attribute, no need to fetch
                sticker_format = sticker_item.format
                
                if hasattr(sticker_item, 'url'):
                    sticker_url_property = sticker_item.url
                
                # Check if format indicates animated sticker (APNG, Lottie, or GIF)
                if isinstance(sticker_format, discord.StickerFormatType):
                    is_animated = sticker_format in (discord.StickerFormatType.apng, discord.StickerFormatType.lottie, discord.StickerFormatType.gif)
                    is_gif_format = sticker_format == discord.StickerFormatType.gif
                    is_lottie_format = sticker_format == discord.StickerFormatType.lottie
                elif hasattr(sticker_format, 'value'):
                    # Enum with value attribute (2=APNG, 3=Lottie, 4=GIF)
                    format_value = sticker_format.value if hasattr(sticker_format, 'value') else sticker_format
                    is_animated = format_value in (2, 3, 4)
                    is_gif_format = format_value == 4  # 4 = GIF
                    is_lottie_format = format_value == 3  # 3 = Lottie
                else:
                    # Fallback: check if format is integer (2=APNG, 3=Lottie, 4=GIF)
                    format_value = sticker_format if isinstance(sticker_format, int) else getattr(sticker_format, 'value', None)
                    if format_value == 4:
                        is_gif_format = True
                        is_animated = True
                    elif format_value == 3:
                        is_lottie_format = True
                        is_animated = True
                
                # DEBUG: Log ALL sticker information
                print(f"\n{'='*60}")
                print(f"Processing Sticker ID: {sticker_id} (DM command)")
                print(f"{'='*60}")
                print(f"StickerItem properties:")
                print(f"  - id: {sticker_item.id}")
                print(f"  - name: {getattr(sticker_item, 'name', 'N/A')}")
                print(f"  - format: {sticker_format}")
                print(f"  - url property: {sticker_url_property}")
                print(f"  - All attributes: {[attr for attr in dir(sticker_item) if not attr.startswith('_')]}")
                
                # Try to fetch full sticker to see what it provides
                try:
                    full_sticker = await sticker_item.fetch()
                    print(f"\nFull Sticker object properties:")
                    print(f"  - id: {full_sticker.id}")
                    print(f"  - name: {getattr(full_sticker, 'name', 'N/A')}")
                    print(f"  - format: {getattr(full_sticker, 'format', 'N/A')}")
                    if hasattr(full_sticker, 'url'):
                        print(f"  - url property: {full_sticker.url}")
                    print(f"  - All attributes: {[attr for attr in dir(full_sticker) if not attr.startswith('_')]}")
                except Exception as fetch_error:
                    print(f"  - Could not fetch full sticker: {fetch_error}")
                
                print(f"\nIs Animated: {is_animated}")
                if is_gif_format:
                    print(f"Format: GIF (Discord native)")
                elif is_lottie_format:
                    print(f"Format: Lottie (JSON animation)")
                else:
                    print(f"Format: APNG (will be converted to GIF)")
            except Exception as e:
                print(f"  - ERROR checking sticker format: {e}")
                import traceback
                traceback.print_exc()
                pass
            
            # For GIF format stickers, use direct Discord CDN URL (Discord supports GIF embeds)
            if is_gif_format and sticker_url_property:
                # Use direct .gif URL with size parameter - Discord supports GIF embeds directly
                base_gif_url = sticker_url_property
                # Remove any existing size parameter
                base_gif_url = re.sub(r'[?&]size=\d+', '', base_gif_url)
                # Add size parameter
                if '?' in base_gif_url:
                    url = f'{base_gif_url}&size={STICKER_DEFAULT_SIZE}'
                else:
                    url = f'{base_gif_url}?size={STICKER_DEFAULT_SIZE}'
                print(f"Using direct GIF URL with size parameter: {url}")
                sticker_results.append({
                    'url': url,
                    'id': sticker_id,
                    'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                    'type': 'sticker_url',
                    'animated': is_animated,
                    'is_gif': True  # Flag to indicate this is a direct GIF URL
                })
                continue
            
            # Handle Lottie stickers separately - provide JSON link only, no processing
            if is_lottie_format:
                # Lottie stickers are not supported - provide JSON link only
                json_url = f'https://cdn.discordapp.com/stickers/{sticker_id}.json'
                sticker_results.append({
                    'url': json_url,
                    'id': sticker_id,
                    'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                    'type': 'sticker_url',
                    'animated': is_animated,
                    'is_lottie': True  # Flag to show unsupported message in embed
                })
                continue
            
            # For APNG animated stickers, use backend server if configured
            # APNG can be converted to GIF for Discord embed compatibility
            if is_animated and not is_gif_format and BACKEND_GUILD_ID and BACKEND_CHANNEL_ID:
                # Get backend URLs (processes and uploads if not cached)
                # Download highest quality version (320px or original)
                if sticker_url_property:
                    # Normalize URL first (convert non-animated PNG to media.discordapp.net)
                    normalized_url = normalize_sticker_url(sticker_url_property, is_animated=is_animated)
                    # Remove any existing size parameter and use highest quality
                    high_quality_url = re.sub(r'[?&]size=\d+', '', normalized_url)
                    if '?' in high_quality_url:
                        original_url = f'{high_quality_url}&size=320'
                    else:
                        original_url = f'{high_quality_url}?size=320'
                else:
                    # Default URL based on format
                    if is_lottie_format:
                        original_url = f'https://cdn.discordapp.com/stickers/{sticker_id}.json?size=320'
                    else:
                        original_url = f'https://media.discordapp.net/stickers/{sticker_id}.png?size=320'
                result = await get_backend_sticker_urls(sticker_id, original_url, is_lottie=is_lottie_format)
                
                if result:
                    backend_urls, message_ids = result
                    if STICKER_DEFAULT_SIZE in backend_urls:
                        # Use backend URL for the default size
                        url = backend_urls[STICKER_DEFAULT_SIZE]
                        print(f"Using backend server URL: {url}")
                        # Store all backend URLs and message IDs in sticker_data
                        sticker_results.append({
                            'url': url,
                            'id': sticker_id,
                            'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                            'type': 'sticker_url',
                            'animated': is_animated,
                            'backend_urls': backend_urls,  # Store all sizes for button switching
                            'uploaded_message_ids': message_ids,  # Store message IDs for deletion
                            'uses_backend': True,  # Flag to indicate using custom server
                            'is_lottie': is_lottie_format  # Store format type for embed messages
                        })
                        continue
                
                print(f"Backend processing failed, falling back to Discord CDN")
            
            # Use Discord's own URL format (cdn.discordapp.com) and add size parameter
            if sticker_url_property:
                # Discord provides: https://cdn.discordapp.com/stickers/{id}.png
                # For non-animated PNG stickers, convert to media.discordapp.net (size parameter works)
                # For animated stickers, cdn.discordapp.com works fine
                base_url = normalize_sticker_url(sticker_url_property, is_animated=is_animated)
                # Check if URL already has parameters
                if '?' in base_url:
                    # Add size parameter to existing query string
                    url = f'{base_url}&size={STICKER_DEFAULT_SIZE}'
                else:
                    # Add size parameter as first query parameter
                    url = f'{base_url}?size={STICKER_DEFAULT_SIZE}'
                print(f"Using Discord's sticker.url (normalized) with size parameter: {url}")
            elif is_animated:
                # Fallback: Use Discord's CDN format with size (works for animated!)
                url = f'https://media.discordapp.net/stickers/{sticker_id}.png?size={STICKER_DEFAULT_SIZE}'
                print(f"Fallback: Using cdn.discordapp.com format: {url}")
            else:
                # For static stickers, use media.discordapp.net with .webp (size parameter works)
                url = f'https://media.discordapp.net/stickers/{sticker_id}.webp?size={STICKER_DEFAULT_SIZE}&quality=lossless'
                print(f"Static sticker: Using media.discordapp.net format: {url}")
            
            print(f"\nFinal URL chosen: {url}")
            print(f"{'='*60}\n")
            sticker_results.append({
                'url': url,
                'id': sticker_id,
                'name': sticker_item.name if hasattr(sticker_item, 'name') else None,
                'type': 'sticker_url',
                'animated': is_animated
            })
    
    # Combine emoji and sticker results
    results = []
    
    # Add emojis
    for match in emoji_matches[:10]:
        animated = bool(match[0])
        name = match[1]
        emoji_id = match[2]
        ext = 'gif' if animated else 'webp'
        url = f'https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=48&quality=lossless'
        results.append({'url': url, 'animated': animated, 'id': emoji_id, 'name': name, 'type': 'discord_emoji'})
    
    # Add stickers
    results.extend(sticker_results)
    
    if not results:
        embed = discord.Embed(
            title="No items found",
            description="This message does not contain any custom emojis or stickers.",
            color=0x2F3136
        )
        embed.add_field(
            name="How to use",
            value=(
                "Right-click a message that contains emojis or stickers,\n"
                "then select **Apps → DM Emoji & Sticker Links**"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    try:
        dm = await interaction.user.create_dm()
        if len(results) == 1:
            parsed = results[0]
            expires_at = None  # DM views never expire
            # Use appropriate default size based on type
            default_size = STICKER_DEFAULT_SIZE if parsed['type'] in ('sticker_url', 'sticker_id') else 48
            embed = build_single_embed(parsed, default_size, expires_at)
            
            # Use different views for stickers vs emojis (DMs are persistent)
            # Lottie stickers don't get size buttons - only JSON link is provided
            view = None
            if parsed['type'] in ('sticker_url', 'sticker_id') and not parsed.get('is_lottie'):
                view = StickerSizeSelectionView(parsed, default_size, persistent=True)
                # Set button styles
                for item in view.children:
                    if isinstance(item, discord.ui.Button):
                        size_str = f"{default_size}px"
                        if size_str in item.label:
                            item.style = discord.ButtonStyle.primary
                        else:
                            item.style = discord.ButtonStyle.secondary
            elif parsed['type'] not in ('sticker_url', 'sticker_id'):
                view = SizeSelectionView(parsed, default_size, persistent=True)
                # Set button styles
                for item in view.children:
                    if isinstance(item, discord.ui.Button):
                        size_str = f"{default_size}px"
                        if size_str in item.label:
                            item.style = discord.ButtonStyle.primary
                        else:
                            item.style = discord.ButtonStyle.secondary
            
            if view:
                sent = await dm.send(embed=embed, view=view)
                view.message = sent
                await view.on_message_set(sent, is_ephemeral=False)
            else:
                sent = await dm.send(embed=embed)
        else:
            expires_at = None  # DM batch view never expires
            embed = build_batch_embed(results, 48, expires_at)
            view = BatchSizeView(results, persistent=True)
            sent = await dm.send(embed=embed, view=view)
            view.message = sent
            await view.on_message_set(sent, is_ephemeral=False)
        await interaction.followup.send(content="Sent to your DMs.", ephemeral=True)
    except discord.Forbidden:
        embed = discord.Embed(
            title="Cannot send DM",
            description="Unable to send you a direct message.",
            color=0x2F3136
        )
        embed.add_field(
            name="How to fix",
            value="Enable **Allow direct messages from server members** in your privacy settings.",
            inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        embed = discord.Embed(
            title="Failed to send DM",
            description="An error occurred while attempting to send you a direct message.",
            color=0x2F3136
        )
        embed.add_field(
            name="Error details",
            value=f"`{str(e)[:200]}{'...' if len(str(e)) > 200 else ''}`",
            inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

def get_sticker_format_type(sticker_format, is_lottie: bool = False, is_gif: bool = False) -> str:
    """Get sticker format type name from format enum.
    
    Args:
        sticker_format: Discord StickerFormatType enum or value
        is_lottie: Whether sticker is Lottie format
        is_gif: Whether sticker is GIF format
    
    Returns:
        Format name string: 'lottie', 'apng', 'png', 'gif', 'webp'
    """
    if is_lottie:
        return 'lottie'
    if is_gif:
        return 'gif'
    
    if isinstance(sticker_format, discord.StickerFormatType):
        if sticker_format == discord.StickerFormatType.png:
            return 'png'
        elif sticker_format == discord.StickerFormatType.apng:
            return 'apng'
        elif sticker_format == discord.StickerFormatType.lottie:
            return 'lottie'
        elif sticker_format == discord.StickerFormatType.gif:
            return 'gif'
        else:
            return 'webp'  # Default
    elif hasattr(sticker_format, 'value'):
        format_value = sticker_format.value
        if format_value == 1:
            return 'png'
        elif format_value == 2:
            return 'apng'
        elif format_value == 3:
            return 'lottie'
        elif format_value == 4:
            return 'gif'
        else:
            return 'webp'
    else:
        # Fallback
        format_value = sticker_format if isinstance(sticker_format, int) else 0
        if format_value == 1:
            return 'png'
        elif format_value == 2:
            return 'apng'
        elif format_value == 3:
            return 'lottie'
        elif format_value == 4:
            return 'gif'
        else:
            return 'webp'

@bot.tree.context_menu(name="Add to My List")
async def add_to_my_list(interaction: discord.Interaction, message: discord.Message):
    """Add emoji or sticker from message to user's personal list."""
    await interaction.response.defer(ephemeral=True)
    
    user_id = str(interaction.user.id)
    saved_items = []
    
    # Check for emojis in message content
    emoji_matches = re.findall(r'<(a)?:(\w+):(\d{15,20})>', message.content)
    
    for match in emoji_matches[:10]:  # Limit to 10 emojis
        animated = bool(match[0])
        name = match[1]
        emoji_id = match[2]
        ext = 'gif' if animated else 'webp'
        url = f'https://cdn.discordapp.com/emojis/{emoji_id}.{ext}?size=48&quality=lossless'
        
        item_data = {
            'url': url,
            'animated': animated,
            'id': emoji_id,
            'name': name,
            'type': 'discord_emoji'
        }
        
        # Check if emoji with same ID already exists
        existing_item = await find_item_by_id(user_id, emoji_id, 'emoji')
        if existing_item:
            # Skip adding, but note it in the response
            saved_items.append(('emoji_duplicate', existing_item['name'], name))
            continue
        
        # Validate emoji URL before saving
        is_valid = await validate_url(url)
        if not is_valid:
            # Skip invalid emoji, note it in response
            saved_items.append(('emoji_invalid', name, emoji_id))
            continue
        
        # Handle duplicate names
        final_name = await save_user_item(user_id, name, 'emoji', item_data)
        saved_items.append(('emoji', final_name))
    
    # Check for stickers in message
    if message.stickers:
        for sticker_item in message.stickers[:10]:  # Limit to 10 stickers
            sticker_id = str(sticker_item.id)
            
            # Get sticker format info
            is_animated = False
            is_gif_format = False
            is_lottie_format = False
            sticker_format = None
            sticker_url_property = None
            
            try:
                sticker_format = sticker_item.format
                if hasattr(sticker_item, 'url'):
                    sticker_url_property = sticker_item.url
                
                if isinstance(sticker_format, discord.StickerFormatType):
                    is_animated = sticker_format in (discord.StickerFormatType.apng, discord.StickerFormatType.lottie, discord.StickerFormatType.gif)
                    is_gif_format = sticker_format == discord.StickerFormatType.gif
                    is_lottie_format = sticker_format == discord.StickerFormatType.lottie
                elif hasattr(sticker_format, 'value'):
                    format_value = sticker_format.value
                    is_animated = format_value in (2, 3, 4)
                    is_gif_format = format_value == 4
                    is_lottie_format = format_value == 3
                else:
                    format_value = sticker_format if isinstance(sticker_format, int) else 0
                    if format_value == 4:
                        is_gif_format = True
                        is_animated = True
                    elif format_value == 3:
                        is_lottie_format = True
                        is_animated = True
            except Exception:
                pass
            
            # Get sticker name
            sticker_name = sticker_item.name if hasattr(sticker_item, 'name') else f"sticker_{sticker_id[:8]}"
            
            # Build URL
            if is_lottie_format:
                url = f'https://cdn.discordapp.com/stickers/{sticker_id}.json'
            elif is_gif_format and sticker_url_property:
                base_gif_url = sticker_url_property
                base_gif_url = re.sub(r'[?&]size=\d+', '', base_gif_url)
                if '?' in base_gif_url:
                    url = f'{base_gif_url}&size={STICKER_DEFAULT_SIZE}'
                else:
                    url = f'{base_gif_url}?size={STICKER_DEFAULT_SIZE}'
            else:
                # Default sticker URL
                url = f'https://media.discordapp.net/stickers/{sticker_id}.png?size={STICKER_DEFAULT_SIZE}'
            
            # Get format type
            format_type = get_sticker_format_type(sticker_format, is_lottie_format, is_gif_format)
            
            item_data = {
                'url': url,
                'id': sticker_id,
                'name': sticker_name,
                'type': 'sticker_url',
                'animated': is_animated,
                'is_lottie': is_lottie_format,
                'is_gif': is_gif_format
            }
            
            # Check if sticker with same ID already exists
            existing_item = await find_item_by_id(user_id, sticker_id, 'sticker')
            if existing_item:
                # Skip adding, but note it in the response
                saved_items.append(('sticker_duplicate', existing_item['name'], sticker_name))
                continue
            
            # Validate sticker URL before saving
            is_valid = await validate_url(url)
            if not is_valid:
                # Skip invalid sticker, note it in response
                saved_items.append(('sticker_invalid', sticker_name, sticker_id))
                continue
            
            # Handle duplicate names
            final_name = await save_user_item(user_id, sticker_name, 'sticker', item_data, format_type)
            saved_items.append(('sticker', final_name))
    
    if not saved_items:
        embed = discord.Embed(
            title="No items found",
            description="This message does not contain any custom emojis or stickers.",
            color=0x2F3136
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    # Separate new items from duplicates and invalid items
    new_items = [item for item in saved_items if not item[0].endswith('_duplicate') and not item[0].endswith('_invalid')]
    duplicate_items = [item for item in saved_items if item[0].endswith('_duplicate')]
    invalid_items = [item for item in saved_items if item[0].endswith('_invalid')]
    
    # Build confirmation message
    if len(new_items) == 1 and len(duplicate_items) == 0 and len(invalid_items) == 0:
        item_type, item_name = new_items[0]
        embed = discord.Embed(
            title="Item added to your list",
            description=f"**{item_type.capitalize()}** `{item_name}` has been added to your personal list.",
            color=0x2F3136
        )
    else:
        emoji_count = sum(1 for t, *_ in new_items if t == 'emoji')
        sticker_count = sum(1 for t, *_ in new_items if t == 'sticker')
        items_list = []
        if emoji_count > 0:
            items_list.append(f"{emoji_count} emoji{'s' if emoji_count > 1 else ''}")
        if sticker_count > 0:
            items_list.append(f"{sticker_count} sticker{'s' if sticker_count > 1 else ''}")
        
        if new_items:
            embed = discord.Embed(
                title="Items added to your list",
                description=f"Added {', '.join(items_list)} to your personal list." if items_list else "Items processed.",
                color=0x2F3136
            )
            # Show first few item names
            if len(new_items) <= 5:
                items_text = '\n'.join([f"• `{name}` ({t})" for t, name, *_ in new_items])
            else:
                items_text = '\n'.join([f"• `{name}` ({t})" for t, name, *_ in new_items[:5]])
                items_text += f"\n*...and {len(new_items) - 5} more*"
            embed.add_field(name="New items", value=items_text, inline=False)
        else:
            embed = discord.Embed(
                title="No new items added",
                description="All items were already in your list or could not be validated.",
                color=0x2F3136
            )
        
        # Show duplicates if any
        if duplicate_items:
            dup_text = []
            for item in duplicate_items:
                item_type = item[0].replace('_duplicate', '')
                existing_name = item[1]
                attempted_name = item[2] if len(item) > 2 else "unknown"
                dup_text.append(f"• {item_type.capitalize()} `{attempted_name}` → already exists as `{existing_name}`")
            
            embed.add_field(
                name="Already in list",
                value='\n'.join(dup_text[:10]) + (f"\n*...and {len(dup_text) - 10} more*" if len(dup_text) > 10 else ""),
                inline=False
            )
        
        # Show invalid items if any
        if invalid_items:
            invalid_text = []
            for item in invalid_items:
                item_type = item[0].replace('_invalid', '')
                item_name = item[1]
                item_id = item[2] if len(item) > 2 else "unknown"
                invalid_text.append(f"• {item_type.capitalize()} `{item_name}` (ID: `{item_id}`) → not accessible")
            
            embed.add_field(
                name="Could not validate",
                value=(
                    '\n'.join(invalid_text[:10]) + (f"\n*...and {len(invalid_text) - 10} more*" if len(invalid_text) > 10 else "") +
                    "\n\n**Troubleshooting:**\n"
                    "• Check if the ID is correct\n"
                    "• Verify the item still exists\n"
                    "• The item may have been deleted"
                ),
                inline=False
            )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

# Slash Commands
@bot.tree.command(name="get-emoji", description="Get emoji or sticker CDN link (ID, <:name:id>, or URL)")
@app_commands.describe(
    id="Emoji or sticker input (ID, <:name:id>, URL, or sticker URL)",
    animated="Whether the emoji/sticker is animated (for raw IDs only)"
)
@app_commands.choices(size=[
    app_commands.Choice(name="24", value=24),
    app_commands.Choice(name="48", value=48),
    app_commands.Choice(name="56", value=56),
    app_commands.Choice(name="128", value=128),
])
async def slash_emoji(interaction: discord.Interaction, id: str, size: Optional[int] = 48, animated: Optional[bool] = None):
    # Try emoji first, then sticker (for auto-detection)
    parsed = parse_discord_emoji(id, animated=animated)
    if not parsed:
        # Try sticker parsing as fallback
        parsed = parse_discord_sticker(id, animated=animated)
    if not parsed:
        embed = discord.Embed(
            title="Invalid format",
            description="The provided input does not match any supported format.",
            color=0x2F3136
        )
        embed.add_field(
            name="Emoji formats",
            value=(
                "• `<:name:id>` or `<a:name:id>`\n"
                "• `752527580485386269` (raw emoji ID)"
            ),
            inline=True
        )
        embed.add_field(
            name="Sticker formats",
            value=(
                "• `https://media.discordapp.net/stickers/{id}.webp`\n"
                "• `123456789012345678` (raw sticker ID)"
            ),
            inline=True
        )
        embed.add_field(
            name="Direct URLs",
            value="• `https://...` (any valid image URL)",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Validate URL - check if emoji/sticker is accessible
    emoji_url = parsed.get('url')
    if emoji_url:
        # Get base URL without size parameter for validation
        base_url = remove_size_param(emoji_url)
        # Remove query parameters for validation
        if '?' in base_url:
            base_url = base_url.split('?')[0]
        
        is_valid = await validate_url(base_url)
        if not is_valid:
            embed = discord.Embed(
                title="Emoji/Sticker not found",
                description="The emoji or sticker doesn't exist or is not accessible. Please check the ID and parameters.",
                color=0x2F3136
            )
            item_id = parsed.get('id', 'unknown')
            embed.add_field(
                name="Troubleshooting",
                value=(
                    f"• Check if the ID `{item_id}` is correct\n"
                    "• Verify the emoji/sticker still exists\n"
                    "• Ensure the `animated` parameter matches the emoji type\n"
                    "• Try using the emoji directly: `<:name:id>`"
                ),
                inline=False
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    # Default to 48px if size not provided
    display_size = size if size is not None else 48
    
    # Apply size to URL
    parsed['url'] = get_emoji_size(parsed['url'], display_size)

    expires_at = int(time.time()) + 300
    embed = build_single_embed(parsed, display_size, expires_at)
    if parsed['type'] == 'raw_id' and 'gif_url' in parsed:
        gif_url_sized = get_emoji_size(parsed['gif_url'], display_size)
        embed.add_field(name="GIF URL", value=f"[Open link]({gif_url_sized})", inline=True)
    
    # Use different views for stickers vs emojis (ephemeral)
    expires_at = int(time.time()) + 300
    if parsed['type'] in ('sticker_url', 'sticker_id'):
        view = StickerSizeSelectionView(parsed, display_size, expires_at, is_ephemeral=True)
    else:
        view = SizeSelectionView(parsed, display_size, expires_at, is_ephemeral=True)
    
    # Set button styles
    for item in view.children:
        if isinstance(item, discord.ui.Button):
            if f"{display_size}px" in item.label:
                item.style = discord.ButtonStyle.primary
            else:
                item.style = discord.ButtonStyle.secondary
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    try:
        sent = await interaction.original_response()
        view.message = sent
        await view.on_message_set(sent, is_ephemeral=True)
    except Exception:
        pass

@bot.tree.command(name="get-sticker", description="Get sticker CDN link (URL or ID)")
@app_commands.describe(
    url="Sticker URL or ID (https://media.discordapp.net/stickers/... or raw ID)",
    animated="Whether the sticker is animated (for raw IDs only)"
)
@app_commands.choices(size=[
    app_commands.Choice(name="24", value=24),
    app_commands.Choice(name="48", value=48),
    app_commands.Choice(name="96", value=96),
    app_commands.Choice(name="160", value=160),
    app_commands.Choice(name="240", value=240),
    app_commands.Choice(name="320", value=320),
])
async def slash_sticker(interaction: discord.Interaction, url: str, size: Optional[int] = 160, animated: Optional[bool] = None):
    parsed = parse_discord_sticker(url, animated=animated)
    if not parsed:
        embed = discord.Embed(
            title="Invalid format",
            description="The provided input does not match a valid sticker format.",
            color=0x2F3136
        )
        embed.add_field(
            name="Supported formats",
            value=(
                       "• `https://media.discordapp.net/stickers/{id}.webp`\n"
                "• `1232626436993450096` (raw sticker ID)"
            ),
            inline=False
        )
        embed.add_field(
            name="Examples",
            value=(
                       "`https://media.discordapp.net/stickers/1232626436993450096.webp`\n"
                "`1232626436993450096`"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Validate URL - check if sticker is accessible
    sticker_url = parsed.get('url')
    if sticker_url:
        # Get base URL without size parameter for validation
        base_url = remove_size_param(sticker_url)
        # Remove query parameters for validation
        if '?' in base_url:
            base_url = base_url.split('?')[0]
        
        is_valid = await validate_url(base_url)
        if not is_valid:
            embed = discord.Embed(
                title="Sticker not found",
                description="The sticker doesn't exist or is not accessible. Please check the ID and parameters.",
                color=0x2F3136
            )
            sticker_id = parsed.get('id', 'unknown')
            embed.add_field(
                name="Troubleshooting",
                value=(
                    f"• Check if the sticker ID `{sticker_id}` is correct\n"
                    "• Verify the sticker still exists\n"
                    "• Ensure the `animated` parameter matches the sticker type\n"
                    "• Try using the sticker URL directly"
                ),
                inline=False
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    # Clamp size to valid sticker range (24-320)
    display_size = max(STICKER_MIN_SIZE, min(STICKER_MAX_SIZE, size if size is not None else STICKER_DEFAULT_SIZE))
    
    # Apply size to URL
    parsed['url'] = get_emoji_size(parsed['url'], display_size)

    expires_at = int(time.time()) + 300
    embed = build_single_embed(parsed, display_size, expires_at)
    
    # Use StickerSizeSelectionView for stickers (ephemeral)
    expires_at = int(time.time()) + 300
    view = StickerSizeSelectionView(parsed, display_size, expires_at, is_ephemeral=True)
    
    # Set button styles
    for item in view.children:
        if isinstance(item, discord.ui.Button):
            if f"{display_size}px" in item.label:
                item.style = discord.ButtonStyle.primary
            else:
                item.style = discord.ButtonStyle.secondary
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    try:
        sent = await interaction.original_response()
        view.message = sent
        await view.on_message_set(sent, is_ephemeral=True)
    except Exception:
        pass

@bot.tree.command(name='batch', description='Convert multiple emoji/sticker IDs to CDN links')
async def slash_batch(interaction: discord.Interaction, emojis: str):
    emoji_inputs = emojis.split()
    results = []
    for emoji_input in emoji_inputs[:10]:
        # Try both emoji and sticker parsing
        parsed = parse_discord_asset(emoji_input)
        if parsed:
            results.append(parsed)
    if not results:
        embed = discord.Embed(
            title="No valid items",
            description="None of the provided inputs matched a valid emoji or sticker format.",
            color=0x2F3136
        )
        embed.add_field(
            name="Emoji formats",
            value=(
                "• `<:name:id>` or `<a:name:id>`\n"
                "• `123456789012345678` (raw emoji ID)"
            ),
            inline=True
        )
        embed.add_field(
            name="Sticker formats",
            value=(
                "• `https://media.discordapp.net/stickers/{id}.webp`\n"
                "• `123456789012345678` (raw sticker ID)"
            ),
            inline=True
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    expires_at = int(time.time()) + 300
    embed = build_batch_embed(results, 48, expires_at)
    view = BatchSizeView(results, expires_at, is_ephemeral=True)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    try:
        sent = await interaction.original_response()
        view.message = sent
        await view.on_message_set(sent, is_ephemeral=True)
    except Exception:
        pass

# Command group for /add
add_group = app_commands.Group(name='add', description='Add emoji or sticker to your personal list')
bot.tree.add_command(add_group)

@add_group.command(name='emoji', description='Add emoji to your personal list')
@app_commands.describe(
    item="Emoji input (ID, <:name:id>, or URL)",
    name="Display name for this emoji (required)"
)
async def slash_add_emoji(interaction: discord.Interaction, item: str, name: str):
    """Add emoji to user's personal list."""
    user_id = str(interaction.user.id)
    
    # Parse emoji input
    parsed = parse_discord_emoji(item)
    if not parsed:
        embed = discord.Embed(
            title="Invalid format",
            description="The provided input does not match any supported emoji format.",
            color=0x2F3136
        )
        embed.add_field(
            name="Emoji formats",
            value=(
                "• `<:name:id>` or `<a:name:id>`\n"
                "• `752527580485386269` (raw emoji ID)\n"
                "• `https://...` (direct URL)"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Validate URL - always check if emoji URL is accessible
    emoji_url = parsed.get('url')
    if emoji_url:
        is_valid = await validate_url(emoji_url)
        if not is_valid:
            embed = discord.Embed(
                title="Emoji not found",
                description="This emoji doesn't exist or is not accessible. Please check the ID and try again.",
                color=0x2F3136
            )
            emoji_id = parsed.get('id', 'unknown')
            embed.add_field(
                name="Troubleshooting",
                value=(
                    f"• Check if the emoji ID `{emoji_id}` is correct\n"
                    "• Verify the emoji still exists in the server\n"
                    "• Try using the emoji directly: `<:name:id>`"
                ),
                inline=False
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    # Check if emoji with same ID already exists
    emoji_id = parsed.get('id')
    if emoji_id:
        existing_item = await find_item_by_id(user_id, emoji_id, 'emoji')
        if existing_item:
            embed = discord.Embed(
                title="Emoji already exists",
                description=f"This emoji is already saved as `{existing_item['name']}` in your list.",
                color=0x2F3136
            )
            embed.add_field(
                name="Existing item",
                value=f"Name: `{existing_item['name']}`",
                inline=False
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    # Save to user's list
    final_name = await save_user_item(user_id, name, 'emoji', parsed)
    
    embed = discord.Embed(
        title="Emoji added",
        description=f"Emoji `{final_name}` has been added to your personal list.",
        color=0x2F3136
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@add_group.command(name='sticker', description='Add sticker to your personal list')
@app_commands.describe(
    item="Sticker input (ID or URL)",
    name="Display name for this sticker (required)"
)
async def slash_add_sticker(interaction: discord.Interaction, item: str, name: str):
    """Add sticker to user's personal list."""
    user_id = str(interaction.user.id)
    
    # Parse sticker input
    parsed = parse_discord_sticker(item)
    if not parsed:
        embed = discord.Embed(
            title="Invalid format",
            description="The provided input does not match any supported sticker format.",
            color=0x2F3136
        )
        embed.add_field(
            name="Sticker formats",
            value=(
                "• `https://media.discordapp.net/stickers/{id}.webp`\n"
                "• `123456789012345678` (raw sticker ID)\n"
                "• `https://...` (direct URL)"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Validate URL - always check if sticker URL is accessible
    sticker_url = parsed.get('url')
    if sticker_url:
        is_valid = await validate_url(sticker_url)
        if not is_valid:
            embed = discord.Embed(
                title="Sticker not found",
                description="This sticker doesn't exist or is not accessible. Please check the ID and try again.",
                color=0x2F3136
            )
            sticker_id = parsed.get('id', 'unknown')
            embed.add_field(
                name="Troubleshooting",
                value=(
                    f"• Check if the sticker ID `{sticker_id}` is correct\n"
                    "• Verify the sticker still exists\n"
                    "• Try using the sticker URL directly"
                ),
                inline=False
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    # Check if sticker with same ID already exists
    sticker_id = parsed.get('id')
    if sticker_id:
        existing_item = await find_item_by_id(user_id, sticker_id, 'sticker')
        if existing_item:
            embed = discord.Embed(
                title="Sticker already exists",
                description=f"This sticker is already saved as `{existing_item['name']}` in your list.",
                color=0x2F3136
            )
            embed.add_field(
                name="Existing item",
                value=f"Name: `{existing_item['name']}`",
                inline=False
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    # Get format type from parsed data
    format_type = None
    if parsed.get('is_lottie'):
        format_type = 'lottie'
    elif parsed.get('is_gif'):
        format_type = 'gif'
    elif parsed.get('animated'):
        format_type = 'apng'
    else:
        # Try to determine from URL
        url = parsed.get('url', '')
        if '.png' in url:
            format_type = 'png'
        elif '.webp' in url:
            format_type = 'webp'
        else:
            format_type = 'webp'  # Default
    
    # Save to user's list
    final_name = await save_user_item(user_id, name, 'sticker', parsed, format_type)
    
    embed = discord.Embed(
        title="Sticker added",
        description=f"Sticker `{final_name}` ({format_type}) has been added to your personal list.",
        color=0x2F3136
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Command group for /remove
remove_group = app_commands.Group(name='remove', description='Remove emoji or sticker from your personal list')
bot.tree.add_command(remove_group)

async def remove_emoji_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete for remove emoji command name parameter."""
    user_id = str(interaction.user.id)
    
    # Fetch user's emoji items
    items = await get_user_items(user_id, 'emoji')
    
    # Filter by current input (check both name and ID)
    current_lower = current.lower().strip() if current else ""
    filtered_items = []
    
    for item in items:
        item_data = item['data']
        item_name = item['name']
        item_id = item_data.get('id', '')
        item_display_name = item_data.get('name', item_name)
        
        # Check if search term matches name or ID
        matches = False
        if not current_lower:
            matches = True
        else:
            if current_lower in item_name.lower() or current_lower in item_display_name.lower():
                matches = True
            elif item_id and current_lower in str(item_id):
                matches = True
        
        if matches:
            # Format: [animated/static] name
            is_animated = item_data.get('animated', False)
            format_str = 'animated' if is_animated else 'static'
            choice_name = f"[{format_str}] {item_name}"
            filtered_items.append(app_commands.Choice(name=choice_name, value=item_name))
    
    # Sort alphabetically if searching, otherwise keep recent-first order
    if current_lower:
        filtered_items.sort(key=lambda x: x.name.lower())
    
    return filtered_items[:25]  # Discord limit

async def remove_sticker_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete for remove sticker command name parameter."""
    user_id = str(interaction.user.id)
    
    # Fetch user's sticker items
    items = await get_user_items(user_id, 'sticker')
    
    # Filter by current input (check both name and ID)
    current_lower = current.lower().strip() if current else ""
    filtered_items = []
    
    for item in items:
        item_data = item['data']
        item_name = item['name']
        item_id = item_data.get('id', '')
        item_display_name = item_data.get('name', item_name)
        
        # Check if search term matches name or ID
        matches = False
        if not current_lower:
            matches = True
        else:
            if current_lower in item_name.lower() or current_lower in item_display_name.lower():
                matches = True
            elif item_id and current_lower in str(item_id):
                matches = True
        
        if matches:
            # Format: [format] name
            format_type = item_data.get('format', 'unknown')
            choice_name = f"[{format_type}] {item_name}"
            filtered_items.append(app_commands.Choice(name=choice_name, value=item_name))
    
    # Sort alphabetically if searching, otherwise keep recent-first order
    if current_lower:
        filtered_items.sort(key=lambda x: x.name.lower())
    
    return filtered_items[:25]  # Discord limit

@remove_group.command(name='emoji', description='Remove emoji from your personal list')
@app_commands.describe(
    name="Name of the emoji to remove"
)
@app_commands.autocomplete(name=remove_emoji_name_autocomplete)
async def slash_remove_emoji(interaction: discord.Interaction, name: str):
    """Remove emoji from user's personal list."""
    user_id = str(interaction.user.id)
    
    # Remove the item
    removed = await remove_user_item(user_id, name, 'emoji')
    
    if removed:
        embed = discord.Embed(
            title="Emoji removed",
            description=f"Emoji `{name}` has been removed from your personal list.",
            color=0x2F3136
        )
    else:
        embed = discord.Embed(
            title="Emoji not found",
            description=f"Emoji `{name}` was not found in your personal list.",
            color=0x2F3136
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@remove_group.command(name='sticker', description='Remove sticker from your personal list')
@app_commands.describe(
    name="Name of the sticker to remove"
)
@app_commands.autocomplete(name=remove_sticker_name_autocomplete)
async def slash_remove_sticker(interaction: discord.Interaction, name: str):
    """Remove sticker from user's personal list."""
    user_id = str(interaction.user.id)
    
    # Remove the item
    removed = await remove_user_item(user_id, name, 'sticker')
    
    if removed:
        embed = discord.Embed(
            title="Sticker removed",
            description=f"Sticker `{name}` has been removed from your personal list.",
            color=0x2F3136
        )
    else:
        embed = discord.Embed(
            title="Sticker not found",
            description=f"Sticker `{name}` was not found in your personal list.",
            color=0x2F3136
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def send_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete for /send name parameter - shows user's saved items filtered by type and search."""
    user_id = str(interaction.user.id)
    
    # Get the selected type from the interaction options
    type_value = None
    try:
        # Access the current value of the 'type' parameter from interaction data
        if hasattr(interaction, 'data') and interaction.data:
            options = interaction.data.get('options', [])
            for option in options:
                if option.get('name') == 'type':
                    type_value = option.get('value')
                    break
        # Fallback: try namespace (might not work during autocomplete)
        if not type_value and hasattr(interaction, 'namespace'):
            type_value = getattr(interaction.namespace, 'type', None)
    except Exception:
        pass
    
    # Only show items when type is selected - strict filtering
    if not type_value:
        # If type not selected yet, return empty list (Discord will show suggestions after type is selected)
        return []
    
    # Get items filtered by type
    all_items = await get_user_items(user_id, type_value)
    
    # Filter by current input (case-insensitive)
    # If current is empty, show all recent items (up to 25)
    # If current has text, filter and show matching items
    current_lower = current.lower().strip() if current else ""
    filtered_items = []
    
    for item in all_items:
        item_name = item['name']
        item_data = item['data']
        item_type = item_data.get('type', '')
        
        # Strict filtering by type - only show matching items
        if type_value:
            if type_value == 'emoji' and item_type not in ('discord_emoji', 'raw_id', 'url'):
                continue
            if type_value == 'sticker' and item_type not in ('sticker_url', 'sticker_id'):
                continue
        
        # Get item details for filtering and display
        item_id = item_data.get('id', '')
        item_display_name = item_data.get('name', item_name)
        
        # Filter by current input - check both name and ID
        matches = False
        if not current_lower:
            matches = True
        else:
            # Check if search term matches name or ID
            if current_lower in item_name.lower() or current_lower in item_display_name.lower():
                matches = True
            elif item_id and current_lower in str(item_id):
                matches = True
        
        if matches:
            # Build display name in format: [format] name
            display_name = item_name
            
            # For emojis, show: [animated/static] name
            if item_type in ('discord_emoji', 'raw_id'):
                is_animated = item_data.get('animated', False)
                format_str = 'animated' if is_animated else 'static'
                display_name = f"[{format_str}] {item_name}"
            
            # For stickers, show: [format] name
            elif item_type in ('sticker_url', 'sticker_id'):
                format_type = item_data.get('format', 'unknown')
                display_name = f"[{format_type}] {item_name}"
            
            # For URL items (fallback)
            else:
                display_name = item_name
            
            filtered_items.append(app_commands.Choice(
                name=display_name[:100],  # Discord limit
                value=item_name
            ))
    
    # If user is searching (typing), sort alphabetically
    # If not searching, keep recent-first order (already sorted from DB)
    if current_lower:
        filtered_items.sort(key=lambda x: x.name.lower())
    # Otherwise, items are already in recent-first order from get_user_items()
    
    # Limit to 25 (Discord's max)
    return filtered_items[:25]

@bot.tree.command(name='send', description='Send a saved emoji or sticker from your list')
@app_commands.describe(
    type="Type of item to send",
    name="Name of the item from your list"
)
@app_commands.choices(type=[
    app_commands.Choice(name="emoji", value="emoji"),
    app_commands.Choice(name="sticker", value="sticker"),
])
@app_commands.autocomplete(name=send_name_autocomplete)
async def slash_send(interaction: discord.Interaction, type: str, name: str):
    """Send a saved emoji or sticker from user's personal list."""
    user_id = str(interaction.user.id)
    
    # Get item from user's list
    item_data = await get_user_item_by_name(user_id, name, type)
    
    if not item_data:
        embed = discord.Embed(
            title="Item not found",
            description=f"No {type} named `{name}` found in your personal list.",
            color=0x2F3136
        )
        embed.add_field(
            name="How to add items",
            value=(
                "• Use `/add emoji` or `/add sticker` commands\n"
                "• Right-click a message → **Add to My List**"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Set default size based on type
    default_size = STICKER_DEFAULT_SIZE if type == 'sticker' else 48
    
    # Apply default size to URL
    item_data['url'] = get_emoji_size(item_data['url'], default_size)
    
    expires_at = int(time.time()) + 300
    embed = build_single_embed(item_data, default_size, expires_at)
    
    # Add GIF URL field if needed
    if item_data.get('type') == 'raw_id' and 'gif_url' in item_data:
        gif_url_sized = get_emoji_size(item_data['gif_url'], default_size)
        embed.add_field(name="GIF URL", value=f"[Open link]({gif_url_sized})", inline=True)
    
    # Use appropriate view based on type
    if item_data.get('type') in ('sticker_url', 'sticker_id'):
        # Skip size buttons for Lottie stickers
        if item_data.get('is_lottie'):
            view = None
        else:
            view = StickerSizeSelectionView(item_data, default_size, expires_at, is_ephemeral=True)
    else:
        view = SizeSelectionView(item_data, default_size, expires_at, is_ephemeral=True)
    
    # Set button styles
    if view:
        for item in view.children:
            if isinstance(item, discord.ui.Button):
                if f"{default_size}px" in item.label:
                    item.style = discord.ButtonStyle.primary
                else:
                    item.style = discord.ButtonStyle.secondary
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    if view:
        try:
            sent = await interaction.original_response()
            view.message = sent
            await view.on_message_set(sent, is_ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name='help', description='Show help and usage')
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Emolink — Help",
        description=(
            "Convert custom emojis and stickers into copyable CDN links.\n"
            "Use buttons and dropdowns to change sizes."
        ),
        color=0x2F3136
    )
    embed.add_field(
        name="Get Links",
        value=(
            "`/get-emoji id:<input>` — Single emoji/sticker (auto-detects)\n"
            "`/get-sticker url:<input>` — Single sticker (explicit)\n"
            "`/batch emojis:<items>` — Multiple items (space-separated)"
        ),
        inline=False
    )
    embed.add_field(
        name="Personal List",
        value=(
            "`/add emoji item:<input> name:<name>` — Save emoji to your list\n"
            "`/add sticker item:<input> name:<name>` — Save sticker to your list\n"
            "`/send type:<emoji/sticker> name:<name>` — Send from your list\n"
            "`/remove emoji name:<name>` — Remove emoji from your list\n"
            "`/remove sticker name:<name>` — Remove sticker from your list"
        ),
        inline=False
    )
    embed.add_field(
        name="Context Menus",
        value=(
            "**Right-click message → Apps →**\n"
            "• **Extract Emoji & Sticker Links** — Get links in channel\n"
            "• **DM Emoji & Sticker Links** — Get links in DM\n"
            "• **Add to My List** — Save emojis/stickers to your list"
        ),
        inline=False
    )
    embed.add_field(
        name="Accepted inputs",
        value=(
            "**Emojis:**\n"
            "• `<:name:id>` or `<a:name:id>`\n"
            "• `123456789012345678` (raw emoji ID)\n\n"
            "**Stickers:**\n"
            "• `https://media.discordapp.net/stickers/{id}.webp`\n"
            "• `123456789012345678` (raw sticker ID)\n\n"
            "**Both:**\n"
            "• `https://...` (direct URL)"
        ),
        inline=True
    )
    embed.add_field(
        name="Examples",
        value=(
            "**Get Links:**\n"
            "• `/get-emoji id:<:cat:752527580485386269>`\n"
            "• `/get-sticker url:123456789012345678`\n\n"
            "**Personal List:**\n"
            "• `/add emoji item:<:cat:752...> name:mycat`\n"
            "• `/send type:emoji name:mycat`\n"
            "• `/remove emoji name:mycat`"
        ),
        inline=True
    )
    embed.add_field(
        name="Size options",
        value=(
            "**Emojis:** Default `48px`\n"
            "Options: `24`, `48`, `56`, `128`, or `Original`\n\n"
            "**Stickers:** Default `160px`\n"
            f"Options: `{', '.join(map(str, STICKER_SIZES))}px`"
        ),
        inline=False
    )
    embed.add_field(
        name="Animation parameter",
        value="For raw IDs, use `animated:true` or `animated:false` to specify animation status.",
        inline=False
    )
    embed.add_field(
        name="Sticker formats",
        value=(
            "• **GIF stickers** → Native Discord support\n"
            "• **APNG stickers** → Auto-converted to GIF\n"
            "• **Lottie stickers** → JSON link provided (manual conversion required)"
        ),
        inline=False
    )
    embed.add_field(
        name="Expiration and persistence",
        value=(
            "• **Ephemeral messages** (slash commands) expire after 5 minutes\n"
            "• **DM messages** (context menu) are persistent — buttons work indefinitely\n"
            "• Persistent views survive bot restarts"
        ),
        inline=False
    )
    embed.set_footer(text="Contact @blankeed for feedback/issues")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='uptime', description='Check how long the bot has been running')
async def slash_uptime(interaction: discord.Interaction):
    if start_time is None:
        embed = discord.Embed(
            title="Bot uptime",
            description="Bot is currently starting up. Please try again in a moment.",
            color=0x2F3136
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    uptime_seconds = get_uptime()
    uptime_formatted = format_uptime(uptime_seconds)
    start_time_formatted = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(start_time))
    
    embed = discord.Embed(
        title="Bot uptime",
        description=f"**Running for:** `{uptime_formatted}`\n**Started:** <t:{int(start_time)}:R>",
        color=0x2F3136
    )
    embed.add_field(name="Uptime", value=f"`{uptime_formatted}`\n`{int(uptime_seconds)}` seconds", inline=True)
    embed.add_field(name="Servers", value=f"`{len(bot.guilds)}` server{'s' if len(bot.guilds) != 1 else ''}", inline=True)
    embed.add_field(name="Started", value=f"<t:{int(start_time)}:F>", inline=False)
    
    try:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.errors.HTTPException as e:
        if e.status == 429:  # Rate limited
            # Try to extract retry_after from response headers or use default
            retry_after = 5.0
            try:
                if hasattr(e, 'response') and e.response:
                    retry_after_header = e.response.headers.get('Retry-After')
                    if retry_after_header:
                        retry_after = float(retry_after_header)
                elif hasattr(e, 'retry_after'):
                    retry_after = float(e.retry_after)
            except (ValueError, AttributeError, TypeError):
                retry_after = 5.0  # Default to 5 seconds
            
            try:
                error_msg = f"⚠️ Rate limited. Please try again in {retry_after:.0f} seconds."
                if interaction.response.is_done():
                    await interaction.followup.send(error_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(error_msg, ephemeral=True)
            except:
                # If we can't send anything, just log it
                print(f"Rate limited on /uptime command. Retry after: {retry_after}s")
        else:
            # Re-raise other HTTP exceptions
            raise

@bot.tree.command(name='status', description='View bot server information and statistics')
async def slash_status(interaction: discord.Interaction):
    """Owner-only command to view bot server information. Only works in DMs."""
    # Immediately check permissions - respond ephemerally if not authorized
    # Check if it's a DM first
    if interaction.guild is not None:
        await interaction.response.send_message("This command can only be used in DMs.", ephemeral=True)
        return
    
    # Check if user is bot owner - do this immediately
    try:
        app_info = await bot.application_info()
        owner_id = app_info.owner.id if app_info.owner else None
        
        if owner_id is None:
            await interaction.response.send_message("Unable to verify bot owner.", ephemeral=True)
            return
            
        if interaction.user.id != owner_id:
            await interaction.response.send_message("This command is only available to the bot owner.", ephemeral=True)
            return
    except Exception as e:
        await interaction.response.send_message("Error verifying permissions.", ephemeral=True)
        print(f"Error checking owner in /status: {e}")
        return
    
    # Get guild information
    guilds_info = []
    total_members = 0
    total_channels = 0
    
    for guild in sorted(bot.guilds, key=lambda g: g.member_count, reverse=True):
        member_count = guild.member_count or 0
        channel_count = len(guild.channels)
        total_members += member_count
        total_channels += channel_count
        
        owner_name = "Unknown"
        if guild.owner:
            owner_name = f"{guild.owner.name}#{guild.owner.discriminator}" if hasattr(guild.owner, 'discriminator') and guild.owner.discriminator != '0' else guild.owner.name
        
        # Try to find who added the bot via audit logs
        inviter_name = "Unknown"
        inviter_id = None
        bot_joined_at = None
        
        try:
            # Get when bot joined
            bot_member = guild.get_member(bot.user.id)
            if bot_member and bot_member.joined_at:
                bot_joined_at = bot_member.joined_at
            
            # Try to get audit logs to find who added the bot
            if guild.me.guild_permissions.view_audit_log:
                async for entry in guild.audit_logs(limit=100, action=discord.AuditLogAction.bot_add):
                    if entry.target and entry.target.id == bot.user.id:
                        inviter_name = f"{entry.user.name}#{entry.user.discriminator}" if hasattr(entry.user, 'discriminator') and entry.user.discriminator != '0' else entry.user.name
                        inviter_id = entry.user.id
                        break
        except Exception as e:
            # No permission or error accessing audit logs
            pass
        
        guilds_info.append({
            'name': guild.name,
            'id': guild.id,
            'members': member_count,
            'channels': channel_count,
            'owner': owner_name,
            'owner_id': guild.owner_id,
            'created': guild.created_at,
            'inviter': inviter_name,
            'inviter_id': inviter_id,
            'bot_joined_at': bot_joined_at
        })
    
    # Build embed
    embed = discord.Embed(
        title="Bot Status & Statistics",
        description="Bot performance metrics and server information",
        color=0x2F3136
    )
    
    # Server summary
    embed.add_field(
        name="Server Summary",
        value=(
            f"**Total Servers:** `{len(bot.guilds)}`\n"
            f"**Total Members:** `{total_members:,}`\n"
            f"**Total Channels:** `{total_channels:,}`"
        ),
        inline=False
    )
    
    # Top 10 servers by member count
    if guilds_info:
        top_servers_text = ""
        for i, guild in enumerate(guilds_info[:10], 1):
            guild_name = guild['name'][:25]  # Truncate long names
            top_servers_text += f"{i}. **{guild_name}**\n"
            top_servers_text += f"   ID: `{guild['id']}` · Members: `{guild['members']:,}`\n"
            top_servers_text += f"   Owner: `{guild['owner']}`\n"
            
            # Show who added the bot if available
            if guild['inviter'] != "Unknown":
                top_servers_text += f"   Added by: `{guild['inviter']}`\n"
            elif guild['bot_joined_at']:
                joined_date = guild['bot_joined_at'].strftime("%Y-%m-%d")
                top_servers_text += f"   Bot joined: `{joined_date}`\n"
            
            top_servers_text += "\n"
        
        if len(guilds_info) > 10:
            top_servers_text += f"*...and {len(guilds_info) - 10} more server(s)*"
        
        embed.add_field(
            name=f"Top Servers (by member count)",
            value=top_servers_text,
            inline=False
        )
        
        # Add a separate field showing authorization summary
        servers_with_inviter = sum(1 for g in guilds_info if g['inviter'] != "Unknown")
        if servers_with_inviter > 0:
            embed.add_field(
                name="Bot Authorization",
                value=(
                    f"**Servers with known inviter:** `{servers_with_inviter}/{len(guilds_info)}`\n"
                    f"*Requires VIEW_AUDIT_LOG permission to display*"
                ),
                inline=False
            )
    
    # Get statistics from databases
    persistent_views_count = 0
    cached_stickers = 0
    cached_urls = 0
    user_saved_items_count = 0
    unique_users_with_items = 0
    
    try:
        async with aiosqlite.connect(PERSISTENT_DB_FILE) as db:
            async with db.execute('SELECT COUNT(*) FROM persistent_views') as cursor:
                persistent_views_count = (await cursor.fetchone())[0] or 0
    except Exception as e:
        print(f"Error reading persistent views count: {e}")
    
    try:
        async with aiosqlite.connect(LOCAL_DB_FILE) as db:
            async with db.execute('SELECT COUNT(DISTINCT sticker_id) FROM sticker_cache') as cursor:
                cached_stickers = (await cursor.fetchone())[0] or 0
            async with db.execute('SELECT COUNT(*) FROM sticker_cache') as cursor:
                cached_urls = (await cursor.fetchone())[0] or 0
    except Exception as e:
        print(f"Error reading cache stats: {e}")
    
    try:
        async with aiosqlite.connect(USER_SAVED_ITEMS_DB) as db:
            async with db.execute('SELECT COUNT(*) FROM user_saved_items') as cursor:
                user_saved_items_count = (await cursor.fetchone())[0] or 0
            async with db.execute('SELECT COUNT(DISTINCT user_id) FROM user_saved_items') as cursor:
                unique_users_with_items = (await cursor.fetchone())[0] or 0
    except Exception as e:
        print(f"Error reading user saved items stats: {e}")
    
    # Database statistics
    embed.add_field(
        name="Storage Statistics",
        value=(
            f"**Persistent Views:** `{persistent_views_count:,}`\n"
            f"**Sticker Cache:** `{cached_stickers:,}` unique entries\n"
            f"**Cached URLs:** `{cached_urls:,}` total\n"
            f"**User Saved Items:** `{user_saved_items_count:,}` items (`{unique_users_with_items:,}` users)"
        ),
        inline=False
    )
    embed.add_field(
        name="Storage Details",
        value=(
            "Persistent views maintain interactive buttons across bot restarts.\n"
            "Sticker cache optimizes performance (contains no personal information).\n"
            "User items are stored only when explicitly saved via `/add` commands."
        ),
        inline=False
    )
    
    # Bot information
    embed.add_field(
        name="Bot Information",
        value=(
            f"**Bot ID:** `{bot.user.id}`\n"
            f"**Bot Name:** {bot.user.name}\n"
            f"**Owner:** {app_info.owner.name if app_info.owner else 'Unknown'}\n"
            f"**Discord.py Version:** `{discord.__version__}`"
        ),
        inline=False
    )
    
    embed.set_footer(text=f"Requested by {interaction.user.name}")
    embed.timestamp = discord.utils.utcnow()
    
    # Add admin control view
    view = AdminControlView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Start the bot
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("DISCORD_TOKEN environment variable not found!")
        print("Please set your Discord bot token in the environment variables.")
        exit(1)
    
    # Start health check server in background thread if enabled
    if HEALTH_CHECK_PORT > 0:
        health_thread = threading.Thread(target=run_health_server, args=(HEALTH_CHECK_PORT,), daemon=True)
        health_thread.start()
    
    # Register shutdown handlers for graceful shutdown
    import atexit
    import signal
    
    def shutdown_handler():
        print("\nShutting down...")
        # Close HTTP session
        try:
            import asyncio
            # Try to get existing event loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Schedule cleanup
                    asyncio.create_task(close_http_session())
                else:
                    loop.run_until_complete(close_http_session())
            except RuntimeError:
                # No event loop, create one
                asyncio.run(close_http_session())
        except Exception as e:
            print(f"Error closing HTTP session: {e}")
        # Firebase Firestore automatically persists data, no explicit save needed
    
    # Also handle SIGINT and SIGTERM for graceful shutdown (cross-platform)
    try:
        def signal_handler(sig, frame):
            shutdown_handler()
            exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        if hasattr(signal, 'SIGTERM'):  # SIGTERM not available on Windows
            signal.signal(signal.SIGTERM, signal_handler)
    except (ImportError, AttributeError):
        # Signal handling not available on this platform
        pass
    
    atexit.register(shutdown_handler)
    
    print("🚀 Starting Emoji CDN Bot...")
    
    # Retry logic for bot startup with exponential backoff
    max_retries = 5
    base_delay = 5  # Start with 5 seconds
    
    for attempt in range(max_retries):
        try:
            bot.run(BOT_TOKEN)
            break  # Success, exit retry loop
        except discord.errors.HTTPException as e:
            if e.status == 429:  # Rate limited
                if attempt < max_retries - 1:
                    # Calculate exponential backoff: 5s, 10s, 20s, 40s, 80s
                    delay = base_delay * (2 ** attempt)
                    
                    # Try to get retry_after from response if available
                    retry_after = delay
                    try:
                        if hasattr(e, 'response') and e.response:
                            retry_after_header = e.response.headers.get('Retry-After')
                            if retry_after_header:
                                retry_after = float(retry_after_header)
                        elif hasattr(e, 'retry_after'):
                            retry_after = float(e.retry_after)
                    except (ValueError, AttributeError, TypeError):
                        pass
                    
                    print(f"⚠️ Rate limited during bot startup (attempt {attempt + 1}/{max_retries})")
                    print(f"⏳ Retrying in {retry_after:.0f} seconds...")
                    time.sleep(retry_after)
                else:
                    print(f"❌ Failed to start bot after {max_retries} attempts due to rate limiting.")
                    print("💡 The bot's IP address is being rate limited by Cloudflare.")
                    print("💡 Please wait a few minutes and try again, or contact your hosting provider.")
                    exit(1)
            else:
                # Other HTTP errors, re-raise
                raise
        except KeyboardInterrupt:
            print("\n⚠️ Bot startup interrupted by user")
            exit(0)
        except Exception as e:
            # Other unexpected errors
            print(f"❌ Unexpected error during bot startup: {e}")
            raise
